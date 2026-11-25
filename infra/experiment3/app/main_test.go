package main

import (
	"container/heap"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

func TestQueueRetainsPayloadAndOrdersReadyTime(t *testing.T) {
	q := jobs{}
	now := time.Now()
	delayed := &job{payload: []byte("retry-payload"), due: now.Add(time.Second)}
	ready := &job{payload: []byte("new-payload"), due: now}
	heap.Push(&q, delayed)
	heap.Push(&q, ready)
	if heap.Pop(&q) != ready {
		t.Fatal("ready request must precede delayed retry")
	}
	if string(q[0].payload) != "retry-payload" {
		t.Fatal("retry must retain original payload")
	}
	heap.Pop(&q)
	if q[:cap(q)][0] != nil {
		t.Fatal("removed job must not be retained by backing array")
	}
}

func TestLateCallbackCannotCompleteDifferentAttempt(t *testing.T) {
	pending = map[string]chan struct{}{"1-1": make(chan struct{}, 1)}
	r := httptest.NewRequest("POST", "/callback", nil)
	r.Header.Set("X-Attempt-ID", "1-0")
	before := late.Load()
	callback(httptest.NewRecorder(), r)
	if late.Load() != before+1 {
		t.Fatal("late callback must be counted")
	}
	if len(pending["1-1"]) != 0 {
		t.Fatal("previous attempt must not complete retry")
	}
	r.Header.Set("X-Attempt-ID", "1-1")
	callback(httptest.NewRecorder(), r)
	if len(pending["1-1"]) != 1 {
		t.Fatal("current callback should complete current attempt")
	}
	pending = map[string]chan struct{}{}
}

func TestService2DropsWithoutQueuingWhenFull(t *testing.T) {
	capacity = make(chan struct{}, 1)
	capacity <- struct{}{}
	before := dropped.Load()
	w := httptest.NewRecorder()
	work(w, httptest.NewRequest("POST", "/work", strings.NewReader("payload")))
	if w.Code != 503 || dropped.Load() != before+1 || len(capacity) != 1 {
		t.Fatal("full service2 must reject without allocating work")
	}
}

func TestSubmitRejectsOversizeAndKeepsAcceptedPayload(t *testing.T) {
	payloadBytes = 4
	queue = nil
	w := httptest.NewRecorder()
	submit(w, httptest.NewRequest("POST", "/submit", strings.NewReader("12345")))
	if w.Code != 400 || len(queue) != 0 {
		t.Fatal("oversize request must not enter queue")
	}
	w = httptest.NewRecorder()
	submit(w, httptest.NewRequest("POST", "/submit", strings.NewReader("1234")))
	if w.Code != 202 || len(queue) != 1 || string(queue[0].payload) != "1234" {
		t.Fatal("accepted request must retain payload in queue")
	}
	queue = nil
}
