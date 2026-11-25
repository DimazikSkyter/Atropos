import com.sun.net.httpserver.HttpServer;
import java.net.*;
import java.net.http.*;
import java.time.Duration;
import java.util.concurrent.*;

/** Exercise real HTTP admission and callback correlation without starting the workload. */
public class MainTest {
    static void require(boolean condition, String message) {
        if (!condition) throw new AssertionError(message);
    }
    public static void main(String[] args) throws Exception {
        HttpServer server = HttpServer.create(new InetSocketAddress("127.0.0.1",0),16);
        server.createContext("/submit",Main::submit);
        server.createContext("/callback",Main::callback);
        server.createContext("/metrics",Main::metrics);
        server.start();
        HttpClient client = HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(2)).build();
        String base = "http://127.0.0.1:"+server.getAddress().getPort();
        try {
            HttpResponse<Void> bad = client.send(HttpRequest.newBuilder(URI.create(base+"/submit"))
                .POST(HttpRequest.BodyPublishers.ofByteArray(new byte[Main.PAYLOAD+1])).build(), HttpResponse.BodyHandlers.discarding());
            require(bad.statusCode()==400 && Main.queue.isEmpty(),"oversize request admitted");
            HttpResponse<Void> ok = client.send(HttpRequest.newBuilder(URI.create(base+"/submit"))
                .POST(HttpRequest.BodyPublishers.ofByteArray(new byte[Main.PAYLOAD])).build(), HttpResponse.BodyHandlers.discarding());
            require(ok.statusCode()==202 && Main.queue.size()==1,"accepted request lost");
            require(Main.queue.take().payload.length==Main.PAYLOAD,"queue did not retain payload");
            CompletableFuture<Void> retry = new CompletableFuture<>();
            Main.pending.put("test-1",retry);
            long before = Main.late.sum();
            client.send(HttpRequest.newBuilder(URI.create(base+"/callback")).header("X-Attempt-ID","test-0")
                .POST(HttpRequest.BodyPublishers.noBody()).build(),HttpResponse.BodyHandlers.discarding());
            require(!retry.isDone() && Main.late.sum()==before+1,"late callback completed another attempt");
            client.send(HttpRequest.newBuilder(URI.create(base+"/callback")).header("X-Attempt-ID","test-1")
                .POST(HttpRequest.BodyPublishers.noBody()).build(),HttpResponse.BodyHandlers.discarding());
            require(retry.isDone(),"current callback did not complete attempt");
            String metrics = client.send(HttpRequest.newBuilder(URI.create(base+"/metrics")).GET().build(),
                HttpResponse.BodyHandlers.ofString()).body();
            require(metrics.contains("generation=\"minor\"") && metrics.contains("generation=\"major\""),"minor/major GC metrics missing");
            Main.Job delayed = new Main.Job(new byte[Main.PAYLOAD]);
            delayed.due += TimeUnit.SECONDS.toNanos(2);
            Main.Job ready = new Main.Job(new byte[Main.PAYLOAD]);
            Main.queue.add(delayed); Main.queue.add(ready);
            require(Main.queue.take()==ready && Main.queue.peek()==delayed,"delay queue ordering broken");
            System.out.println("PASS: admission, payload retention, callback correlation, GC and delay queue");
        } finally { server.stop(0); Main.queue.clear(); Main.pending.clear(); client.close(); }
    }
}
