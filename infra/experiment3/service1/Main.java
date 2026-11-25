import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;
import java.io.*;
import java.lang.management.*;
import java.net.*;
import java.net.http.*;
import java.nio.charset.StandardCharsets;
import java.nio.file.*;
import java.time.Duration;
import java.util.*;
import java.util.concurrent.*;
import java.util.concurrent.atomic.*;

/** A deliberately unbounded queue retains payloads when the HTTP callback pool saturates. */
public class Main {
    static final int SLOTS = env("CLIENT_SLOTS", 48);
    static final int PAYLOAD = env("PAYLOAD_BYTES", 4096);
    static final DelayQueue<Job> queue = new DelayQueue<>();
    static final ConcurrentHashMap<String, CompletableFuture<Void>> pending = new ConcurrentHashMap<>();
    static final AtomicLong sequence = new AtomicLong();
    static final LongAdder received = new LongAdder(), attempts = new LongAdder(), retries = new LongAdder(),
        timeouts = new LongAdder(), completed = new LongAdder(), failed = new LongAdder(),
        callbacks = new LongAdder(), late = new LongAdder(), errors = new LongAdder();
    static final AtomicInteger active = new AtomicInteger();
    static final HttpClient client = HttpClient.newBuilder().version(HttpClient.Version.HTTP_1_1)
        .executor(Executors.newFixedThreadPool(8)).connectTimeout(Duration.ofMillis(50)).build();

    static int env(String key, int fallback) {
        int n = Integer.parseInt(System.getenv().getOrDefault(key, "" + fallback));
        if (n <= 0) throw new IllegalArgumentException(key);
        return n;
    }
    static class Job implements Delayed {
        final long id = sequence.incrementAndGet(), created = System.nanoTime();
        final byte[] payload;
        int attempt;
        long due = created;
        Job(byte[] payload) { this.payload = payload; }
        public long getDelay(TimeUnit unit) { return unit.convert(due - System.nanoTime(), TimeUnit.NANOSECONDS); }
        public int compareTo(Delayed other) { return Long.compare(due, ((Job) other).due); }
    }
    static void reply(HttpExchange x, int code) throws IOException {
        x.sendResponseHeaders(code, -1);
        x.close();
    }
    static void submit(HttpExchange x) throws IOException {
        if (!x.getRequestMethod().equals("POST")) { reply(x,405); return; }
        byte[] data = x.getRequestBody().readNBytes(PAYLOAD + 1);
        if (data.length != PAYLOAD) { reply(x,400); return; }
        queue.add(new Job(data)); received.increment(); reply(x,202);
    }
    static void callback(HttpExchange x) throws IOException {
        callbacks.increment();
        String key = x.getRequestHeaders().getFirst("X-Attempt-ID");
        CompletableFuture<Void> promise = key == null ? null : pending.get(key);
        if (promise == null) late.increment(); else promise.complete(null);
        reply(x,204);
    }
    static void attempt(Job job) {
        attempts.increment(); if (job.attempt > 0) retries.increment();
        String key = job.id + "-" + job.attempt;
        CompletableFuture<Void> promise = new CompletableFuture<>();
        pending.put(key,promise);
        long deadline = System.nanoTime() + TimeUnit.MILLISECONDS.toNanos(50);
        boolean ok = false;
        try {
            HttpRequest request = HttpRequest.newBuilder(URI.create("http://service2:8080/work"))
                .header("X-Attempt-ID",key).timeout(Duration.ofMillis(50))
                .POST(HttpRequest.BodyPublishers.ofByteArray(job.payload)).build();
            HttpResponse<Void> response = client.send(request, HttpResponse.BodyHandlers.discarding());
            if (response.statusCode() == 202) {
                long remaining = deadline - System.nanoTime();
                if (remaining <= 0) throw new TimeoutException();
                promise.get(remaining, TimeUnit.NANOSECONDS);
                ok = true;
            } else errors.increment();
        } catch (HttpTimeoutException | TimeoutException e) {
            timeouts.increment();
        } catch (Exception e) {
            errors.increment();
            if (e instanceof InterruptedException) Thread.currentThread().interrupt();
        } finally { pending.remove(key); }
        if (ok) { completed.increment(); return; }
        if (job.attempt < 2) {
            job.attempt++;
            job.due = System.nanoTime() + TimeUnit.SECONDS.toNanos(job.attempt);
            queue.add(job);
        } else failed.increment();
    }
    static void worker() {
        while (!Thread.currentThread().isInterrupted()) {
            try {
                Job job = queue.take();
                active.incrementAndGet();
                try { attempt(job); } finally { active.decrementAndGet(); }
            } catch (InterruptedException e) { Thread.currentThread().interrupt(); }
        }
    }
    static void metric(StringBuilder b, String name, String type, long value) {
        b.append("# TYPE exp3_").append(name).append(' ').append(type).append('\n')
            .append("exp3_").append(name).append("{service=\"service1\"} ").append(value).append('\n');
    }
    static long cgroup(String modern, String legacy) {
        for (String path : new String[]{modern,legacy}) {
            try { return Long.parseLong(Files.readString(Path.of(path)).trim()); }
            catch (Exception ignored) {}
        }
        return 0;
    }
    static void metrics(HttpExchange x) throws IOException {
        StringBuilder b = new StringBuilder(4096);
        String[] names = {"received","attempts","retries","timeouts","completed","failed","callbacks","late_callbacks","transport_errors"};
        LongAdder[] counters = {received,attempts,retries,timeouts,completed,failed,callbacks,late,errors};
        for (int i = 0; i < names.length; i++) metric(b,names[i]+"_total","counter",counters[i].sum());
        metric(b,"active","gauge",active.get()); metric(b,"capacity","gauge",SLOTS);
        metric(b,"payload_bytes","gauge",PAYLOAD); metric(b,"queue_depth","gauge",queue.size());
        Job head = queue.peek();
        metric(b,"queue_head_age_ms","gauge",head==null ? 0 : TimeUnit.NANOSECONDS.toMillis(System.nanoTime()-head.created));
        MemoryUsage heap = ManagementFactory.getMemoryMXBean().getHeapMemoryUsage();
        metric(b,"heap_bytes","gauge",heap.getUsed()); metric(b,"heap_max_bytes","gauge",heap.getMax());
        metric(b,"container_memory_bytes","gauge",cgroup("/sys/fs/cgroup/memory.current","/sys/fs/cgroup/memory/memory.usage_in_bytes"));
        metric(b,"container_memory_limit_bytes","gauge",cgroup("/sys/fs/cgroup/memory.max","/sys/fs/cgroup/memory/memory.limit_in_bytes"));
        metric(b,"jvm_threads","gauge",ManagementFactory.getThreadMXBean().getThreadCount());
        for (MemoryPoolMXBean pool : ManagementFactory.getMemoryPoolMXBeans()) {
            if (pool.getType() == MemoryType.HEAP) {
                String name = pool.getName();
                b.append("exp3_jvm_memory_pool_bytes{service=\"service1\",pool=\"").append(name)
                    .append("\"} ").append(pool.getUsage().getUsed()).append('\n');
            }
        }
        for (GarbageCollectorMXBean gc : ManagementFactory.getGarbageCollectorMXBeans()) {
            String gen = gc.getName().equals("PS Scavenge") ? "minor" : "major";
            b.append("exp3_jvm_gc_collections_total{service=\"service1\",generation=\"").append(gen)
                .append("\"} ").append(Math.max(0,gc.getCollectionCount())).append('\n');
            b.append("exp3_jvm_gc_collection_seconds_total{service=\"service1\",generation=\"").append(gen)
                .append("\"} ").append(Math.max(0,gc.getCollectionTime())/1000.0).append('\n');
        }
        byte[] bytes = b.toString().getBytes(StandardCharsets.UTF_8);
        x.getResponseHeaders().set("Content-Type","text/plain; version=0.0.4");
        x.sendResponseHeaders(200, bytes.length); x.getResponseBody().write(bytes); x.close();
    }
    public static void main(String[] args) throws Exception {
        HttpServer server = HttpServer.create(new InetSocketAddress(8080), 256);
        server.setExecutor(Executors.newFixedThreadPool(16));
        server.createContext("/health",x -> reply(x,204));
        server.createContext("/submit",Main::submit);
        server.createContext("/callback",Main::callback);
        server.createContext("/metrics",Main::metrics);
        for (int i=0; i<SLOTS; i++) Thread.ofPlatform().name("callback-client-"+i).start(Main::worker);
        server.start();
        System.out.println("service1: Parallel GC, heap max=128MiB, client slots="+SLOTS);
    }
}
