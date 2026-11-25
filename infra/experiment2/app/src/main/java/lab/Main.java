package lab;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.github.benmanes.caffeine.cache.Cache;
import com.github.benmanes.caffeine.cache.Caffeine;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;
import io.micrometer.core.instrument.Counter;
import io.micrometer.core.instrument.Gauge;
import io.micrometer.core.instrument.Timer;
import io.micrometer.core.instrument.binder.jvm.JvmMemoryMetrics;
import io.micrometer.core.instrument.binder.system.ProcessorMetrics;
import io.micrometer.prometheusmetrics.PrometheusConfig;
import io.micrometer.prometheusmetrics.PrometheusMeterRegistry;
import org.apache.kafka.clients.admin.Admin;
import org.apache.kafka.clients.admin.OffsetSpec;
import org.apache.kafka.clients.consumer.*;
import org.apache.kafka.clients.producer.*;
import org.apache.kafka.common.TopicPartition;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.sql.*;
import java.time.Duration;
import java.util.*;
import java.util.concurrent.*;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicLong;

public class Main {
    static final String ROLE = env("ROLE", "api"), NODE = env("NODE", "node"), TOPIC = "cache-requests", GROUP = "cache-readers";
    static final String BROKERS = "kafka1:9092,kafka2:9092,kafka3:9092";
    static final PrometheusMeterRegistry REG = new PrometheusMeterRegistry(PrometheusConfig.DEFAULT);
    static final ObjectMapper JSON = new ObjectMapper();
    static final AtomicInteger ASSIGNED = new AtomicInteger();
    static final Set<Integer> PARTITIONS = ConcurrentHashMap.newKeySet();
    static volatile boolean ready = false;
    static KafkaProducer<String,String> producer;
    static final ConcurrentHashMap<Integer,AtomicLong> PHASE_START = new ConcurrentHashMap<>();
    static final Cache<String,String> CACHE = Caffeine.newBuilder().maximumSize(Integer.parseInt(env("CACHE_SIZE", "256"))).recordStats().build();
    public record Event(int client, int entity, long sentMs, int phase) {}
    static String env(String key, String fallback) { return System.getenv().getOrDefault(key, fallback); }
    static Timer timer(String name, String... tags) {
        return Timer.builder(name).tags(tags).publishPercentileHistogram()
            .minimumExpectedValue(Duration.ofNanos(1000)).maximumExpectedValue(Duration.ofSeconds(30)).register(REG);
    }
    static void reply(HttpExchange x, int code, String body) throws Exception {
        byte[] bytes = body.getBytes(StandardCharsets.UTF_8);
        x.sendResponseHeaders(code, bytes.length);
        try(var out = x.getResponseBody()) { out.write(bytes); }
    }
    public static void main(String[] args) throws Exception {
        REG.config().commonTags("node", NODE, "role", ROLE);
        new ProcessorMetrics().bindTo(REG);
        new JvmMemoryMetrics().bindTo(REG);
        Gauge.builder("exp2.assigned.partitions", ASSIGNED, AtomicInteger::get).register(REG);
        Gauge.builder("exp2.ready", () -> ready ? 1 : 0).register(REG);
        HttpServer server = HttpServer.create(new InetSocketAddress(8080), 128);
        server.setExecutor(Executors.newFixedThreadPool(16));
        server.createContext("/metrics", x -> {
            try { x.getResponseHeaders().set("Content-Type", "text/plain; version=0.0.4"); reply(x,200,REG.scrape()); }
            catch(Exception e) { x.close(); }
        });
        server.createContext("/health", x -> {
            try { reply(x,ready ? 200 : 503,JSON.writeValueAsString(Map.of("ready",ready,"partitions",PARTITIONS))); }
            catch(Exception e) { x.close(); }
        });
        if (ROLE.equals("api")) {
            Properties p = props();
            p.put("key.serializer","org.apache.kafka.common.serialization.StringSerializer");
            p.put("value.serializer","org.apache.kafka.common.serialization.StringSerializer");
            p.put("acks","all"); p.put("enable.idempotence","true");
            p.put("delivery.timeout.ms","10000"); p.put("request.timeout.ms","5000"); p.put("max.block.ms","10000");
            producer = new KafkaProducer<>(p);
            producer.partitionsFor(TOPIC);
            server.createContext("/request", Main::request);
            ready = true;
            startLagProbe();
        } else {
            Thread worker = new Thread(() -> {
                try { consume(); } catch(Exception e) { ready = false; REG.counter("exp2.consumer.errors").increment(); e.printStackTrace(); }
            }, "consumer");
            worker.start();
        }
        server.start();
    }
    static Properties props() { Properties p = new Properties(); p.put("bootstrap.servers",BROKERS); p.put("client.id",NODE); return p; }
    static void request(HttpExchange x) {
        long start = System.nanoTime(); String result = "error";
        try {
            if (!x.getRequestMethod().equals("POST")) { reply(x,405,"POST required"); return; }
            int client = Integer.parseInt(x.getRequestHeaders().getFirst("X-Client-Id"));
            var input = JSON.readTree(x.getRequestBody());
            int entity = input.path("entity").asInt(-1), phase = input.path("phase").asInt(-1);
            if(client<0 || client>128 || entity<0 || entity>=4096 || phase<0 || phase>3) {
                reply(x,400,"invalid client/entity/phase"); return;
            }
            int partition = client == 0 ? 0 : 1;
            Event event = new Event(client, entity, System.currentTimeMillis(), phase);
            AtomicLong marker=PHASE_START.computeIfAbsent(phase, v -> {
                AtomicLong clock=new AtomicLong();
                Gauge.builder("exp2.phase.start.seconds",clock,c -> c.get()/1000.0).tag("phase",String.valueOf(v)).register(REG);
                return clock;
            });
            marker.compareAndSet(0,event.sentMs());
            producer.send(new ProducerRecord<>(TOPIC, partition, String.valueOf(client), JSON.writeValueAsString(event))).get(12,TimeUnit.SECONDS);
            REG.counter("exp2.produced", "partition",String.valueOf(partition),"phase",String.valueOf(phase)).increment();
            reply(x,202,"accepted"); result = "ok";
        } catch(Exception e) {
            try { reply(x,500,e.getClass().getSimpleName()); } catch(Exception ignored) { x.close(); }
        } finally { timer("exp2.api", "result", result).record(System.nanoTime()-start,TimeUnit.NANOSECONDS); }
    }
    static void consume() throws Exception {
        Properties p=props(); p.put("group.id",GROUP); p.put("group.instance.id",NODE);
        p.put("key.deserializer","org.apache.kafka.common.serialization.StringDeserializer");
        p.put("value.deserializer","org.apache.kafka.common.serialization.StringDeserializer");
        p.put("enable.auto.commit","false"); p.put("auto.offset.reset","earliest");
        p.put("max.poll.records","100"); p.put("session.timeout.ms","10000");
        p.put("partition.assignment.strategy","org.apache.kafka.clients.consumer.RangeAssignor");
        Gauge.builder("exp2.cache.size",CACHE,Cache::estimatedSize).register(REG);
        Gauge.builder("exp2.cache.evictions",CACHE,c -> c.stats().evictionCount()).register(REG);
        try(Connection db = DriverManager.getConnection("jdbc:postgresql://postgres:5432/experiment2","lab","lab");
            PreparedStatement sql = db.prepareStatement("SELECT md5(string_agg(md5(payload || ? || g::text), '')) FROM entities CROSS JOIN generate_series(1, ?) g WHERE id = ?");
            KafkaConsumer<String,String> consumer = new KafkaConsumer<>(p)) {
            sql.setQueryTimeout(10);
            consumer.subscribe(List.of(TOPIC),new ConsumerRebalanceListener() {
                public void onPartitionsRevoked(Collection<TopicPartition> ps) { ready=false; ASSIGNED.set(0); PARTITIONS.clear(); }
                public void onPartitionsAssigned(Collection<TopicPartition> ps) { ps.forEach(t -> PARTITIONS.add(t.partition())); ASSIGNED.set(ps.size()); ready=ps.size()==1; }
            });
            long lastCommit=System.nanoTime();
            while(true) {
                var records=consumer.poll(Duration.ofMillis(100));
                for(var record:records) {
                    Event event=JSON.readValue(record.value(),Event.class);
                    String partition=String.valueOf(record.partition());
                    long start=System.nanoTime(); String key=event.client()+":"+event.entity();
                    String value=CACHE.getIfPresent(key); String outcome=value==null ? "miss" : "hit";
                    if(value==null) {
                        long dbStart=System.nanoTime();
                        sql.setString(1,String.valueOf(event.client())); sql.setInt(2,Integer.parseInt(env("DB_WORK","2000"))); sql.setInt(3,event.entity());
                        try(ResultSet rs=sql.executeQuery()) { if(!rs.next() || (value=rs.getString(1))==null) throw new IllegalStateException("missing entity"); }
                        timer("exp2.db", "partition",partition).record(System.nanoTime()-dbStart,TimeUnit.NANOSECONDS);
                        CACHE.put(key,value);
                    }
                    long elapsed=System.nanoTime()-start;
                    timer("exp2.processing","partition",partition,"outcome",outcome).record(elapsed,TimeUnit.NANOSECONDS);
                    REG.counter("exp2.processing.squared.seconds","partition",partition).increment(Math.pow(elapsed/1e9,2));
                    REG.counter("exp2.cache.access","partition",partition,"outcome",outcome).increment();
                    REG.counter("exp2.consumed","partition",partition,"phase",String.valueOf(event.phase())).increment();
                    timer("exp2.end.to.end","partition",partition).record(Math.max(0,System.currentTimeMillis()-event.sentMs()),TimeUnit.MILLISECONDS);
                }
                if(System.nanoTime()-lastCommit>500_000_000L) { consumer.commitSync(); lastCommit=System.nanoTime(); }
            }
        }
    }
    static void startLagProbe() {
        var lag = new ConcurrentHashMap<TopicPartition,Double>();
        var probe = new AtomicInteger();
        for(int i=0;i<2;i++) { var tp=new TopicPartition(TOPIC,i); lag.put(tp,Double.NaN);
            Gauge.builder("exp2.kafka.lag",lag,m -> m.get(tp)).tag("partition",String.valueOf(i)).register(REG); }
        Gauge.builder("exp2.kafka.probe.ok",probe,AtomicInteger::get).register(REG);
        Admin admin=Admin.create(props());
        Executors.newSingleThreadScheduledExecutor().scheduleWithFixedDelay(() -> {
            try {
                var ends=admin.listOffsets(Map.of(new TopicPartition(TOPIC,0),OffsetSpec.latest(),new TopicPartition(TOPIC,1),OffsetSpec.latest())).all().get(3,TimeUnit.SECONDS);
                var offsets=admin.listConsumerGroupOffsets(GROUP).partitionsToOffsetAndMetadata().get(3,TimeUnit.SECONDS);
                ends.forEach((tp,v) -> lag.put(tp,(double)Math.max(0,v.offset()-(offsets.containsKey(tp)?offsets.get(tp).offset():0))));
                probe.set(1);
            } catch(Exception e) { probe.set(0); lag.replaceAll((tp,v) -> Double.NaN); }
        },0,2,TimeUnit.SECONDS);
    }
}
