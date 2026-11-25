package lab;

import com.sun.management.GarbageCollectionNotificationInfo;
import com.sun.management.OperatingSystemMXBean;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;
import io.micrometer.core.instrument.FunctionCounter;
import io.micrometer.core.instrument.Gauge;
import io.micrometer.core.instrument.Tags;
import io.micrometer.core.instrument.Timer;
import io.micrometer.prometheusmetrics.PrometheusConfig;
import io.micrometer.prometheusmetrics.PrometheusMeterRegistry;

import javax.management.NotificationEmitter;
import javax.management.openmbean.CompositeData;
import java.io.BufferedWriter;
import java.io.IOException;
import java.lang.management.GarbageCollectorMXBean;
import java.lang.management.ManagementFactory;
import java.lang.management.MemoryPoolMXBean;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardOpenOption;
import java.time.Duration;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.ArrayBlockingQueue;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ThreadPoolExecutor;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicLong;
import java.util.concurrent.atomic.DoubleAdder;
import java.util.concurrent.locks.LockSupport;

/** Fixed request work; allocation is the only changing input during an experiment. */
public final class Main implements AutoCloseable {
    private static final double MIB = 1024.0 * 1024.0;
    private static final int ALLOCATION_BYTES = 4096;
    private static final int MAX_RETAINED_BYTES = 48 * 1024 * 1024;
    private static final Tags SERVICE = Tags.of("service", "java");
    private static final Tags REQUEST_TAGS = SERVICE.and("status", "200");
    // A volatile root makes every array reachable to the GC, even when it is never read again.
    private static volatile byte[][] liveAllocations;
    private static volatile long checksumSink;

    private final long baselineSeconds = envLong("BASELINE_SECONDS", 180, 1, 86_400);
    private final long rampSeconds = envLong("RAMP_SECONDS", 900, 1, 86_400);
    private final long holdSeconds = envLong("HOLD_SECONDS", 180, 1, 86_400);
    private final double allocationStartMiB = envDouble("ALLOC_START_MIB", 16, 0, 4096);
    private final double allocationEndMiB = envDouble("ALLOC_END_MIB", 640, 0, 4096);
    private final long retainNanos = TimeUnit.MILLISECONDS.toNanos(envLong("RETAIN_MILLIS", 60, 1, 1000));
    private final long fixedWorkNanos = Math.round(envDouble("FIXED_WORK_MILLIS", 8, 0.1, 1000) * 1_000_000);
    private final java.lang.management.ThreadMXBean threads = ManagementFactory.getThreadMXBean();
    // Micrometer gauges/function counters weakly reference their observed state.
    private final List<GarbageCollectorMXBean> gcBeans = ManagementFactory.getGarbageCollectorMXBeans();
    private final List<MemoryPoolMXBean> memoryPools = ManagementFactory.getMemoryPoolMXBeans();
    private final java.lang.management.MemoryMXBean memory = ManagementFactory.getMemoryMXBean();
    private final OperatingSystemMXBean os = (OperatingSystemMXBean) ManagementFactory.getOperatingSystemMXBean();
    private final long anchorNano;
    private final double anchorUptimeMillis;
    private final AtomicLong experimentStartNano = new AtomicLong();
    private volatile double startTimeUnixSeconds;
    private volatile boolean running = true;
    private volatile long retainedBytes;
    private volatile double processCpuUsage;
    private volatile double throttledSeconds;
    private volatile double throttledPeriods;
    private final AtomicLong allocatedBytes = new AtomicLong();
    private final AtomicInteger requestsInflight = new AtomicInteger();
    private final DoubleAdder durationSecondsSquared = new DoubleAdder();
    private final PrometheusMeterRegistry registry = new PrometheusMeterRegistry(PrometheusConfig.DEFAULT);
    private final Timer workTimer;
    private final ThreadPoolExecutor executor;
    private final HttpServer server;
    private final CsvLog requestsLog;
    private final CsvLog gcLog;
    private final Thread allocationThread;
    private final Thread samplerThread;

    public static void main(String[] args) throws Exception {
        if (args.length == 1 && args[0].equals("--self-test")) {
            selfTest();
            return;
        }
        Main app = new Main();
        Runtime.getRuntime().addShutdownHook(new Thread(app::close, "shutdown"));
        app.start();
        new CountDownLatch(1).await();
    }

    private Main() throws IOException {
        if (allocationEndMiB < allocationStartMiB) {
            throw new IllegalArgumentException("ALLOC_END_MIB must be >= ALLOC_START_MIB");
        }
        if (!threads.isCurrentThreadCpuTimeSupported()) {
            throw new IllegalStateException("Thread CPU time is required for fixed foreground CPU work");
        }
        if (!threads.isThreadCpuTimeEnabled()) threads.setThreadCpuTimeEnabled(true);
        if (!threads.isThreadCpuTimeEnabled() || threads.getCurrentThreadCpuTime() < 0) {
            throw new IllegalStateException("Thread CPU time could not be enabled");
        }
        long before = System.nanoTime();
        anchorUptimeMillis = ManagementFactory.getRuntimeMXBean().getUptime();
        anchorNano = before + (System.nanoTime() - before) / 2;
        Path results = Path.of(System.getenv().getOrDefault("RESULTS_DIR", "/results"));
        Files.createDirectories(results);
        requestsLog = new CsvLog(results.resolve("requests.csv"), "start_ms,end_ms,duration_ms", "requests");
        gcLog = new CsvLog(results.resolve("gc-events.csv"), "kind,start_ms,end_ms,duration_ms", "gc");
        int workers = (int) envLong("WORK_THREADS", 32, 1, 256);
        executor = new ThreadPoolExecutor(workers, workers, 0, TimeUnit.SECONDS,
                new ArrayBlockingQueue<>(1024), new ThreadPoolExecutor.AbortPolicy());
        server = HttpServer.create(new InetSocketAddress((int) envLong("PORT", 8080, 1, 65535)), 256);
        server.setExecutor(executor);
        workTimer = createWorkTimer(registry);
        registerMetrics();
        subscribeGcNotifications();
        server.createContext("/work", this::work);
        server.createContext("/metrics", e -> send(e, 200, "text/plain; version=0.0.4; charset=utf-8", registry.scrape()));
        server.createContext("/health", e -> send(e, 200, "text/plain", "ok\n"));
        server.createContext("/start", this::startExperiment);
        server.createContext("/status", e -> send(e, 200, "application/json", statusJson()));
        allocationThread = new Thread(this::allocate, "allocation-driver");
        samplerThread = new Thread(this::sampleProcess, "process-sampler");
    }

    private void start() {
        requestsLog.start();
        gcLog.start();
        allocationThread.start();
        samplerThread.start();
        server.start();
        System.out.printf("Ready on :%d; fixed thread CPU budget %.3f ms; baseline %ds, ramp %ds, hold %ds; allocation %.1f→%.1f MiB/s, retention %.1f ms; POST /start to begin%n",
                server.getAddress().getPort(), fixedWorkNanos / 1e6, baselineSeconds, rampSeconds, holdSeconds,
                allocationStartMiB, allocationEndMiB, retainNanos / 1e6);
    }

    private static Timer createWorkTimer(PrometheusMeterRegistry registry) {
        return Timer.builder("exp4.request.duration")
                .description("Actual HTTP handler duration through response close; excludes dispatcher queue time")
                .tags(REQUEST_TAGS).serviceLevelObjectives(bucketDurations()).register(registry);
    }

    private static Duration[] bucketDurations() {
        List<Duration> buckets = new ArrayList<>();
        buckets.add(Duration.ofNanos(500_000));
        for (int millis : new int[]{1, 2, 4}) buckets.add(Duration.ofMillis(millis));
        for (int millis = 5; millis <= 50; millis++) buckets.add(Duration.ofMillis(millis));
        for (int millis : new int[]{60, 75, 100, 150, 250, 500, 1000}) buckets.add(Duration.ofMillis(millis));
        return buckets.toArray(Duration[]::new);
    }

    private void work(HttpExchange exchange) throws IOException {
        if (!exchange.getRequestMethod().equals("GET")) {
            send(exchange, 405, "text/plain", "GET required\n");
            return;
        }
        long begin = System.nanoTime();
        requestsInflight.incrementAndGet();
        boolean success = false;
        try {
            // A fixed CPU budget makes a GC pause add to wall time, instead of being
            // absorbed by a sleep deadline. Neither the budget nor work changes by phase.
            long cpuDeadline = threads.getCurrentThreadCpuTime() + fixedWorkNanos;
            long checksum = 0x9e3779b97f4a7c15L;
            while (threads.getCurrentThreadCpuTime() < cpuDeadline) {
                for (int i = 0; i < 4096; i++) checksum = Long.rotateLeft(checksum ^ i, 7) * 0x100000001b3L;
                checksumSink = checksum;
            }
            send(exchange, 200, "text/plain", "ok " + checksum + "\n");
            success = true;
        } finally {
            long end = System.nanoTime();
            requestsInflight.decrementAndGet();
            if (success) {
                long nanos = end - begin;
                workTimer.record(nanos, TimeUnit.NANOSECONDS);
                double seconds = nanos / 1e9;
                durationSecondsSquared.add(seconds * seconds);
                requestsLog.offer(uptimeMillis(begin) + "," + uptimeMillis(end) + "," + nanos / 1e6);
            }
            exchange.close();
        }
    }

    private void startExperiment(HttpExchange exchange) throws IOException {
        if (!exchange.getRequestMethod().equals("POST")) {
            send(exchange, 405, "text/plain", "POST required\n");
            return;
        }
        synchronized (experimentStartNano) {
            if (experimentStartNano.get() != 0) {
                send(exchange, 409, "application/json", "{\"error\":\"experiment already started; restart container for a new run\"}\n");
                return;
            }
            startTimeUnixSeconds = System.currentTimeMillis() / 1000.0;
            experimentStartNano.set(System.nanoTime());
        }
        send(exchange, 200, "application/json", statusJson());
    }

    private double elapsedSeconds() {
        long start = experimentStartNano.get();
        return start == 0 ? 0 : (System.nanoTime() - start) / 1e9;
    }

    private String phase() {
        if (experimentStartNano.get() == 0) return "idle";
        double elapsed = elapsedSeconds();
        if (elapsed < baselineSeconds) return "baseline";
        if (elapsed < baselineSeconds + rampSeconds) return "ramp";
        if (elapsed < baselineSeconds + rampSeconds + holdSeconds) return "hold";
        return "finished";
    }

    private int phaseNumber() {
        return switch (phase()) { case "baseline" -> 1; case "ramp" -> 2; case "hold" -> 3; case "finished" -> 4; default -> 0; };
    }

    private double allocationTargetMiB() {
        if (experimentStartNano.get() == 0) return 0;
        double elapsed = elapsedSeconds();
        if (elapsed < baselineSeconds) return allocationStartMiB;
        if (elapsed < baselineSeconds + rampSeconds) {
            return allocationStartMiB + (allocationEndMiB - allocationStartMiB) * (elapsed - baselineSeconds) / rampSeconds;
        }
        return elapsed < baselineSeconds + rampSeconds + holdSeconds ? allocationEndMiB : allocationStartMiB;
    }

    private String statusJson() {
        long start = experimentStartNano.get();
        return "{\"elapsed_seconds\":" + elapsedSeconds() + ",\"phase\":\"" + phase()
                + "\",\"baseline_seconds\":" + baselineSeconds + ",\"ramp_seconds\":" + rampSeconds
                + ",\"hold_seconds\":" + holdSeconds + ",\"allocation_target_mib_per_second\":" + allocationTargetMiB()
                + ",\"experiment_start_uptime_ms\":" + (start == 0 ? 0 : uptimeMillis(start))
                + ",\"start_time_unix_seconds\":" + startTimeUnixSeconds
                + ",\"fixed_work_millis\":" + fixedWorkNanos / 1e6
                + ",\"work_model\":\"thread_cpu_budget\"}\n";
    }

    private double uptimeMillis(long nanos) {
        return anchorUptimeMillis + (nanos - anchorNano) / 1e6;
    }

    private void allocate() {
        byte[][] ring = new byte[MAX_RETAINED_BYTES / ALLOCATION_BYTES][];
        long[] expiry = new long[ring.length];
        liveAllocations = ring;
        long head = 0, tail = 0, sequence = 0;
        long last = System.nanoTime();
        double credit = 0;
        while (running) {
            long now = System.nanoTime();
            // Bound catch-up after a long pause; achieved rate is independently observable.
            double seconds = Math.min(0.020, Math.max(0, (now - last) / 1e9));
            last = now;
            credit += allocationTargetMiB() * MIB * seconds;
            while (tail < head && expiry[(int) (tail % ring.length)] <= now) {
                ring[(int) (tail++ % ring.length)] = null;
            }
            int arrays = (int) (credit / ALLOCATION_BYTES);
            credit -= (long) arrays * ALLOCATION_BYTES;
            for (int i = 0; i < arrays && running; i++) {
                if (head - tail == ring.length) ring[(int) (tail++ % ring.length)] = null;
                byte[] bytes = new byte[ALLOCATION_BYTES];
                byte value = (byte) ++sequence;
                for (int offset = 0; offset < bytes.length; offset += 64) bytes[offset] = value;
                bytes[bytes.length - 1] = value;
                int slot = (int) (head++ % ring.length);
                ring[slot] = bytes;
                expiry[slot] = System.nanoTime() + retainNanos;
            }
            allocatedBytes.addAndGet((long) arrays * ALLOCATION_BYTES);
            retainedBytes = (head - tail) * ALLOCATION_BYTES;
            LockSupport.parkNanos(1_000_000);
        }
        liveAllocations = null;
        retainedBytes = 0;
    }

    private void registerMetrics() {
        Gauge.builder("exp4.allocation.target.mib.per.second", this, Main::allocationTargetMiB).tags(SERVICE).register(registry);
        FunctionCounter.builder("exp4.allocated", allocatedBytes, AtomicLong::get).baseUnit("bytes").tags(SERVICE).register(registry);
        Gauge.builder("exp4.retained", this, app -> app.retainedBytes).baseUnit("bytes").tags(SERVICE).register(registry);
        Gauge.builder("exp4.request.duration.seconds.squared.sum", durationSecondsSquared, DoubleAdder::sum)
                .description("Cumulative sum of squared successful durations in seconds squared; use delta/rate for variance")
                .tags(REQUEST_TAGS).register(registry);
        Gauge.builder("exp4.experiment.phase", this, Main::phaseNumber).tags(SERVICE).register(registry);
        Gauge.builder("exp4.experiment.elapsed", this, Main::elapsedSeconds).baseUnit("seconds").tags(SERVICE).register(registry);
        Gauge.builder("exp4.experiment.start.time.unix", this, app -> app.startTimeUnixSeconds).baseUnit("seconds").tags(SERVICE).register(registry);
        Gauge.builder("exp4.requests.inflight", requestsInflight, AtomicInteger::get).tags(SERVICE).register(registry);
        Gauge.builder("exp4.http.executor.queued.tasks", executor, pool -> pool.getQueue().size()).tags(SERVICE).register(registry);
        Gauge.builder("exp4.http.executor.active.threads", executor, ThreadPoolExecutor::getActiveCount).tags(SERVICE).register(registry);
        Gauge.builder("exp4.jvm.heap.used", memory, bean -> bean.getHeapMemoryUsage().getUsed()).baseUnit("bytes").tags(SERVICE).register(registry);
        Gauge.builder("exp4.jvm.heap.committed", memory, bean -> bean.getHeapMemoryUsage().getCommitted()).baseUnit("bytes").tags(SERVICE).register(registry);
        Gauge.builder("exp4.jvm.heap.max", memory, bean -> bean.getHeapMemoryUsage().getMax()).baseUnit("bytes").tags(SERVICE).register(registry);
        for (MemoryPoolMXBean pool : memoryPools) {
            Gauge.builder("exp4.jvm.memory.pool.used", pool, p -> p.getUsage().getUsed()).baseUnit("bytes").tags(SERVICE.and("pool", pool.getName())).register(registry);
        }
        for (String generation : new String[]{"minor", "major"}) {
            FunctionCounter.builder("exp4.gc.collections", gcBeans,
                            beans -> beans.stream().filter(bean -> generation(bean.getName()).equals(generation)).mapToDouble(bean -> Math.max(0, bean.getCollectionCount())).sum())
                    .tags(SERVICE.and("generation", generation)).register(registry);
            FunctionCounter.builder("exp4.gc.pause", gcBeans,
                            beans -> beans.stream().filter(bean -> generation(bean.getName()).equals(generation)).mapToDouble(bean -> Math.max(0, bean.getCollectionTime()) / 1000.0).sum())
                    .baseUnit("seconds").tags(SERVICE.and("generation", generation)).register(registry);
        }
        Gauge.builder("exp4.process.cpu.usage", this, app -> app.processCpuUsage).tags(SERVICE).register(registry);
        FunctionCounter.builder("exp4.process.cpu", os, bean -> Math.max(0, bean.getProcessCpuTime()) / 1e9).baseUnit("seconds").tags(SERVICE).register(registry);
        FunctionCounter.builder("exp4.cgroup.cpu.throttled", this, app -> app.throttledSeconds).baseUnit("seconds").tags(SERVICE).register(registry);
        FunctionCounter.builder("exp4.cgroup.cpu.throttled.periods", this, app -> app.throttledPeriods).tags(SERVICE).register(registry);
        for (CsvLog log : List.of(requestsLog, gcLog)) {
            FunctionCounter.builder("exp4.logs.dropped", log, l -> l.dropped.get()).tags(SERVICE.and("file", log.kind)).register(registry);
        }
    }

    private static String generation(String collector) {
        String name = collector.toLowerCase(java.util.Locale.ROOT);
        return name.contains("old") || name.contains("marksweep") || name.contains("mark sweep") || name.contains("full") || name.contains("cycle") ? "major" : "minor";
    }

    private void subscribeGcNotifications() {
        for (GarbageCollectorMXBean bean : gcBeans) {
            if (bean instanceof NotificationEmitter emitter) {
                emitter.addNotificationListener((notification, ignored) -> {
                    if (!notification.getType().equals(GarbageCollectionNotificationInfo.GARBAGE_COLLECTION_NOTIFICATION)) return;
                    var info = GarbageCollectionNotificationInfo.from((CompositeData) notification.getUserData());
                    var gc = info.getGcInfo();
                    gcLog.offer(generation(info.getGcName()) + "," + gc.getStartTime() + "," + gc.getEndTime() + "," + gc.getDuration());
                }, null, null);
            }
        }
    }

    private void sampleProcess() {
        Path cpuStat = null;
        for (String candidate : List.of("/sys/fs/cgroup/cpu.stat", "/sys/fs/cgroup/cpu/cpu.stat", "/sys/fs/cgroup/cpu,cpuacct/cpu.stat")) {
            if (Files.isRegularFile(Path.of(candidate))) { cpuStat = Path.of(candidate); break; }
        }
        while (running) {
            processCpuUsage = Math.max(0, os.getProcessCpuLoad());
            if (cpuStat != null) {
                try {
                    for (String line : Files.readAllLines(cpuStat)) {
                        String[] value = line.trim().split("\\s+");
                        if (value.length != 2) continue;
                        if (value[0].equals("throttled_usec")) throttledSeconds = Double.parseDouble(value[1]) / 1e6;
                        if (value[0].equals("throttled_time")) throttledSeconds = Double.parseDouble(value[1]) / 1e9;
                        if (value[0].equals("nr_throttled")) throttledPeriods = Double.parseDouble(value[1]);
                    }
                } catch (IOException | NumberFormatException e) {
                    System.err.println("Cannot read cpu.stat: " + e.getMessage());
                }
            }
            LockSupport.parkNanos(TimeUnit.SECONDS.toNanos(1));
        }
    }

    private static void send(HttpExchange exchange, int status, String contentType, String body) throws IOException {
        byte[] response = body.getBytes(StandardCharsets.UTF_8);
        exchange.getResponseHeaders().set("Content-Type", contentType);
        exchange.sendResponseHeaders(status, response.length);
        try (var stream = exchange.getResponseBody()) { stream.write(response); }
    }

    @Override public void close() {
        running = false;
        server.stop(0);
        executor.shutdownNow();
        LockSupport.unpark(allocationThread);
        LockSupport.unpark(samplerThread);
        try {
            allocationThread.join(2000);
            samplerThread.join(2000);
            executor.awaitTermination(2, TimeUnit.SECONDS);
        } catch (InterruptedException e) { Thread.currentThread().interrupt(); }
        requestsLog.close();
        gcLog.close();
        registry.close();
    }

    private static long envLong(String key, long fallback, long minimum, long maximum) {
        long value = Long.parseLong(System.getenv().getOrDefault(key, Long.toString(fallback)));
        if (value < minimum || value > maximum) throw new IllegalArgumentException(key + " must be in [" + minimum + "," + maximum + "]");
        return value;
    }

    private static double envDouble(String key, double fallback, double minimum, double maximum) {
        double value = Double.parseDouble(System.getenv().getOrDefault(key, Double.toString(fallback)));
        if (!Double.isFinite(value) || value < minimum || value > maximum) throw new IllegalArgumentException(key + " must be in [" + minimum + "," + maximum + "]");
        return value;
    }

    /** A deterministic check of actual Micrometer exposition, without hand-written buckets. */
    private static void selfTest() {
        var registry = new PrometheusMeterRegistry(PrometheusConfig.DEFAULT);
        try {
            Timer timer = createWorkTimer(registry);
            timer.record(8, TimeUnit.MILLISECONDS);
            timer.record(21, TimeUnit.MILLISECONDS);
            String scrape = registry.scrape();
            java.util.Map<Double, Double> actualBuckets = new java.util.HashMap<>();
            var boundaryPattern = java.util.regex.Pattern.compile("le=\"([^\"]+)\"");
            for (String line : scrape.split("\\n")) {
                if (!line.startsWith("exp4_request_duration_seconds_bucket{") || !line.contains("service=\"java\"") || !line.contains("status=\"200\"")) continue;
                var match = boundaryPattern.matcher(line);
                if (!match.find()) throw new AssertionError("Bucket has no le label: " + line);
                double boundary = match.group(1).equals("+Inf") ? Double.POSITIVE_INFINITY : Double.parseDouble(match.group(1));
                actualBuckets.put(boundary, Double.parseDouble(line.substring(line.lastIndexOf(' ') + 1)));
            }
            for (Duration duration : bucketDurations()) {
                double boundary = duration.toNanos() / 1e9;
                if (!actualBuckets.containsKey(boundary)) throw new AssertionError("Missing bucket " + boundary + "\n" + scrape);
                double expected = boundary >= 0.021 ? 2 : boundary >= 0.008 ? 1 : 0;
                if (actualBuckets.get(boundary) != expected) throw new AssertionError("Incorrect cumulative count for " + boundary + "\n" + scrape);
            }
            if (!scrape.contains("exp4_request_duration_seconds_count{service=\"java\",status=\"200\"} 2")) throw new AssertionError(scrape);
            if (!Double.valueOf(2).equals(actualBuckets.get(Double.POSITIVE_INFINITY))) throw new AssertionError("Incorrect +Inf bucket\n" + scrape);
            System.out.println("Micrometer self-test passed: 57 finite SLO boundaries + Inf, count=2, measured samples 8 and 21 ms");
        } finally {
            registry.close();
        }
    }

    private static final class CsvLog implements AutoCloseable {
        private final ArrayBlockingQueue<String> queue = new ArrayBlockingQueue<>(65_536);
        private final AtomicLong dropped = new AtomicLong();
        private final String kind;
        private final BufferedWriter writer;
        private final Thread thread;
        private volatile boolean open = true;

        private CsvLog(Path file, String header, String kind) throws IOException {
            this.kind = kind;
            writer = Files.newBufferedWriter(file, StandardCharsets.UTF_8, StandardOpenOption.CREATE, StandardOpenOption.TRUNCATE_EXISTING);
            writer.write(header);
            writer.newLine();
            writer.flush();
            thread = new Thread(this::writeLoop, "csv-" + kind);
        }

        private void start() { thread.start(); }
        private void offer(String line) { if (!queue.offer(line)) dropped.incrementAndGet(); }

        private void writeLoop() {
            long lastFlush = System.nanoTime();
            try {
                while (open || !queue.isEmpty()) {
                    String line = queue.poll(100, TimeUnit.MILLISECONDS);
                    if (line != null) { writer.write(line); writer.newLine(); }
                    if (System.nanoTime() - lastFlush >= TimeUnit.SECONDS.toNanos(1)) {
                        writer.flush();
                        lastFlush = System.nanoTime();
                    }
                }
                writer.flush();
            } catch (IOException e) {
                dropped.addAndGet(queue.size());
                System.err.println("CSV logger " + kind + " failed: " + e.getMessage());
            } catch (InterruptedException e) { Thread.currentThread().interrupt(); }
            finally {
                try { writer.close(); } catch (IOException e) { System.err.println("CSV close failed: " + e.getMessage()); }
            }
        }

        @Override public void close() {
            open = false;
            try { thread.join(3000); } catch (InterruptedException e) { Thread.currentThread().interrupt(); }
        }
    }
}
