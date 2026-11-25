package main

import (
	"bytes"
	"container/heap"
	"context"
	"fmt"
	"io"
	"log"
	"math/rand"
	"net/http"
	"os"
	"regexp"
	"runtime"
	"strconv"
	"sync"
	"sync/atomic"
	"time"
)

func envInt(k string, d int) int {
	if s := os.Getenv(k); s != "" {
		n, e := strconv.Atoi(s)
		if e != nil || n <= 0 {
			log.Fatalf("invalid %s", k)
		}
		return n
	}
	return d
}

type job struct {
	id      uint64
	attempt int
	payload []byte
	due     time.Time
	created time.Time
}
type jobs []*job

func (q jobs) Len() int           { return len(q) }
func (q jobs) Less(i, j int) bool { return q[i].due.Before(q[j].due) }
func (q jobs) Swap(i, j int)      { q[i], q[j] = q[j], q[i] }
func (q *jobs) Push(x any)        { *q = append(*q, x.(*job)) }
func (q *jobs) Pop() any {
	a := *q
	n := len(a) - 1
	x := a[n]
	a[n] = nil
	*q = a[:n]
	return x
}

var lock sync.Mutex
var queue jobs
var pending = map[string]chan struct{}{}
var wake = make(chan struct{}, 1)
var seq atomic.Uint64
var received, attempts, retries, timeouts, completed, failed, callbacks, late, dropped, errors atomic.Int64
var active atomic.Int64
var degraded atomic.Bool
var started atomic.Bool
var phaseStart atomic.Int64
var latencySum atomic.Int64
var latencyCount atomic.Int64
var buckets = []int64{30, 35, 40, 45, 50, 55, 60, 75, 100, 250}
var bucketCounts [10]atomic.Int64
var role string
var gcCount [2]atomic.Int64
var gcPauseMicros [2]atomic.Int64
var gcLogAlive atomic.Bool
var gcPattern = regexp.MustCompile(`GC\([0-9]+\) Pause (Young|Full).* ([0-9.]+)ms`)

// Export from the shared log so the final Full GC remains observable after JVM OOM.
func gcLogs() {
	for {
		f, e := os.Open("/logs/service1-gc.log")
		if e == nil {
			b, _ := io.ReadAll(io.LimitReader(f, 8*1024*1024))
			f.Close()
			var count, pause [2]int64
			for _, match := range gcPattern.FindAllSubmatch(b, -1) {
				i := 0
				if string(match[1]) == "Full" {
					i = 1
				}
				v, _ := strconv.ParseFloat(string(match[2]), 64)
				count[i]++
				pause[i] += int64(v * 1000)
			}
			for i := 0; i < 2; i++ {
				gcCount[i].Store(count[i])
				gcPauseMicros[i].Store(pause[i])
			}
			gcLogAlive.Store(true)
		}
		time.Sleep(time.Second)
	}
}

var slots, payloadBytes, baseline int
var client = &http.Client{Transport: &http.Transport{MaxIdleConns: 256, MaxIdleConnsPerHost: 256}, Timeout: 250 * time.Millisecond}

func enqueue(j *job) {
	lock.Lock()
	heap.Push(&queue, j)
	lock.Unlock()
	select {
	case wake <- struct{}{}:
	default:
	}
}
func take() *job {
	for {
		lock.Lock()
		if len(queue) > 0 {
			j := queue[0]
			wait := time.Until(j.due)
			if wait <= 0 {
				heap.Pop(&queue)
				lock.Unlock()
				return j
			}
			lock.Unlock()
			t := time.NewTimer(wait)
			select {
			case <-wake:
			case <-t.C:
			}
			t.Stop()
		} else {
			lock.Unlock()
			<-wake
		}
	}
}

func worker() {
	for {
		j := take()
		active.Add(1)
		doAttempt(j)
		active.Add(-1)
	}
}
func doAttempt(j *job) {
	attempts.Add(1)
	if j.attempt > 0 {
		retries.Add(1)
	}
	key := fmt.Sprintf("%d-%d", j.id, j.attempt)
	done := make(chan struct{}, 1)
	lock.Lock()
	pending[key] = done
	lock.Unlock()
	// One 50 ms deadline covers submission AND receipt of the HTTP callback.
	ctx, cancel := context.WithTimeout(context.Background(), 50*time.Millisecond)
	defer cancel()
	req, _ := http.NewRequestWithContext(ctx, "POST", "http://service2:8080/work", bytes.NewReader(j.payload))
	req.Header.Set("X-Attempt-ID", key)
	resp, err := client.Do(req)
	ok := false
	if err == nil {
		io.Copy(io.Discard, resp.Body)
		resp.Body.Close()
		if resp.StatusCode == 202 {
			select {
			case <-done:
				ok = true
			case <-ctx.Done():
			}
		} else {
			errors.Add(1)
		}
	} else if ctx.Err() == nil {
		errors.Add(1)
	}
	lock.Lock()
	delete(pending, key)
	lock.Unlock()
	if ok {
		completed.Add(1)
		return
	}
	if ctx.Err() != nil {
		timeouts.Add(1)
	}
	if j.attempt < 2 {
		j.attempt++
		j.due = time.Now().Add(time.Duration(j.attempt) * time.Second)
		enqueue(j)
	} else {
		failed.Add(1)
	}
}
func callback(w http.ResponseWriter, r *http.Request) {
	callbacks.Add(1)
	key := r.Header.Get("X-Attempt-ID")
	lock.Lock()
	ch, ok := pending[key]
	if ok {
		select {
		case ch <- struct{}{}:
		default:
		}
	}
	lock.Unlock()
	if !ok {
		late.Add(1)
	}
	w.WriteHeader(204)
}
func submit(w http.ResponseWriter, r *http.Request) {
	if r.Method != "POST" {
		w.WriteHeader(405)
		return
	}
	data, e := io.ReadAll(http.MaxBytesReader(w, r.Body, int64(payloadBytes)))
	if e != nil || len(data) != payloadBytes {
		http.Error(w, "payload must match PAYLOAD_BYTES", 400)
		return
	}
	received.Add(1)
	now := time.Now()
	enqueue(&job{id: seq.Add(1), payload: data, due: now, created: now})
	w.WriteHeader(202)
}

var capacity chan struct{}

func work(w http.ResponseWriter, r *http.Request) {
	if r.Method != "POST" {
		w.WriteHeader(405)
		return
	}
	received.Add(1)
	select {
	case capacity <- struct{}{}:
	default:
		dropped.Add(1)
		w.WriteHeader(503)
		return
	}
	_, e := io.Copy(io.Discard, http.MaxBytesReader(w, r.Body, int64(payloadBytes)))
	if e != nil {
		<-capacity
		w.WriteHeader(400)
		return
	}
	key := r.Header.Get("X-Attempt-ID")
	active.Add(1)
	w.WriteHeader(202)
	go func() {
		defer func() { <-capacity; active.Add(-1) }()
		start := time.Now()
		lo, hi := 30, 40
		if degraded.Load() {
			lo, hi = 40, 60
		}
		time.Sleep(time.Duration(lo+rand.Intn(hi-lo+1)) * time.Millisecond)
		elapsed := time.Since(start).Microseconds()
		latencySum.Add(elapsed)
		latencyCount.Add(1)
		for i, b := range buckets {
			if elapsed <= b*1000 {
				bucketCounts[i].Add(1)
			}
		}
		req, _ := http.NewRequest("POST", "http://service1:8080/callback", nil)
		req.Header.Set("X-Attempt-ID", key)
		resp, e := client.Do(req)
		if e != nil {
			errors.Add(1)
		} else {
			io.Copy(io.Discard, resp.Body)
			resp.Body.Close()
			callbacks.Add(1)
		}
		completed.Add(1)
	}()
}
func start(w http.ResponseWriter, r *http.Request) {
	if r.Method != "POST" {
		w.WriteHeader(405)
		return
	}
	if started.CompareAndSwap(false, true) {
		phaseStart.Store(time.Now().Unix())
		go func() {
			time.Sleep(time.Duration(baseline) * time.Second)
			degraded.Store(true)
			log.Printf("DEGRADATION: delay 40..60 ms")
		}()
	}
	w.WriteHeader(204)
}
func metrics(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Type", "text/plain; version=0.0.4")
	label := fmt.Sprintf("{service=%q}", role)
	counter := func(n string, v int64) { fmt.Fprintf(w, "# TYPE exp3_%s counter\nexp3_%s%s %d\n", n, n, label, v) }
	gauge := func(n string, v int64) { fmt.Fprintf(w, "# TYPE exp3_%s gauge\nexp3_%s%s %d\n", n, n, label, v) }
	counter("received_total", received.Load())
	counter("attempts_total", attempts.Load())
	counter("retries_total", retries.Load())
	counter("timeouts_total", timeouts.Load())
	counter("completed_total", completed.Load())
	counter("failed_total", failed.Load())
	counter("callbacks_total", callbacks.Load())
	counter("late_callbacks_total", late.Load())
	counter("dropped_total", dropped.Load())
	counter("transport_errors_total", errors.Load())
	if role == "service2" {
		fmt.Fprintln(w, "# TYPE exp3_gc_events_total counter\n# TYPE exp3_gc_pause_seconds_total counter")
		for i, g := range []string{"minor", "major"} {
			fmt.Fprintf(w, "exp3_gc_events_total{service=\"service1\",generation=%q,source=\"gc-log\"} %d\nexp3_gc_pause_seconds_total{service=\"service1\",generation=%q,source=\"gc-log\"} %.6f\n", g, gcCount[i].Load(), g, float64(gcPauseMicros[i].Load())/1e6)
		}
		var alive int64
		if gcLogAlive.Load() {
			alive = 1
		}
		gauge("gc_log_readable", alive)
	}
	gauge("active", active.Load())
	gauge("capacity", int64(slots))
	gauge("payload_bytes", int64(payloadBytes))
	gauge("phase_start_seconds", phaseStart.Load())
	var d int64
	if degraded.Load() {
		d = 1
	}
	gauge("degraded", d)
	lock.Lock()
	gauge("queue_depth", int64(len(queue)))
	var age int64
	if len(queue) > 0 {
		age = time.Since(queue[0].created).Milliseconds()
	}
	lock.Unlock()
	gauge("queue_head_age_ms", age)
	var m runtime.MemStats
	runtime.ReadMemStats(&m)
	gauge("heap_bytes", int64(m.HeapAlloc))
	gauge("runtime_sys_bytes", int64(m.Sys))
	gauge("goroutines", int64(runtime.NumGoroutine()))
	// cgroup v2 memory includes all resident allocations, including the retained queue.
	for _, v := range []struct{ name, path, legacy string }{{"container_memory_bytes", "/sys/fs/cgroup/memory.current", "/sys/fs/cgroup/memory/memory.usage_in_bytes"}, {"container_memory_limit_bytes", "/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes"}} {
		b, e := os.ReadFile(v.path)
		if e != nil {
			b, e = os.ReadFile(v.legacy)
		}
		if e == nil {
			n, e := strconv.ParseInt(string(bytes.TrimSpace(b)), 10, 64)
			if e == nil {
				gauge(v.name, n)
			}
		}
	}
	fmt.Fprintln(w, "# TYPE exp3_processing_seconds histogram")
	for i, b := range buckets {
		fmt.Fprintf(w, "exp3_processing_seconds_bucket{service=%q,le=%q} %d\n", role, fmt.Sprintf("%.3f", float64(b)/1000), bucketCounts[i].Load())
	}
	fmt.Fprintf(w, "exp3_processing_seconds_bucket{service=%q,le=\"+Inf\"} %d\nexp3_processing_seconds_sum%s %.6f\nexp3_processing_seconds_count%s %d\n", role, latencyCount.Load(), label, float64(latencySum.Load())/1e6, label, latencyCount.Load())
}
func main() {
	role = os.Getenv("ROLE")
	slots = envInt("CLIENT_SLOTS", 48)
	payloadBytes = envInt("PAYLOAD_BYTES", 4096)
	baseline = envInt("BASELINE_SECONDS", 60)
	http.HandleFunc("/health", func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(204) })
	http.HandleFunc("/metrics", metrics)
	if role == "service1" {
		http.HandleFunc("/submit", submit)
		http.HandleFunc("/callback", callback)
		for i := 0; i < slots; i++ {
			go worker()
		}
	} else if role == "service2" {
		slots = envInt("SERVER_SLOTS", 128)
		capacity = make(chan struct{}, slots)
		http.HandleFunc("/work", work)
		http.HandleFunc("/start", start)
		go gcLogs()
	} else {
		log.Fatal("ROLE must be service1 or service2")
	}
	log.Printf("%s started, slots=%d", role, slots)
	log.Fatal((&http.Server{Addr: ":8080", ReadHeaderTimeout: 2 * time.Second, ReadTimeout: 2 * time.Second, WriteTimeout: 2 * time.Second, IdleTimeout: 30 * time.Second}).ListenAndServe())
}
