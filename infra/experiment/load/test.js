import http from 'k6/http';
import exec from 'k6/execution';
import { check } from 'k6';
import { Rate } from 'k6/metrics';

const success = new Rate('entity_success');
const rate = Number(__ENV.RATE || 40);
const duration = __ENV.DURATION || '300s';
// Each scenario gets enough VUs for its entire HTTP timeout plus scheduling headroom.
const vus = Number(__ENV.VUS || Math.max(100, Math.ceil(rate / 2) * 20 + 50));
const run = __ENV.RUN_ID || 'manual';
const base = ['http://api1:8000', 'http://api2:8000'];
const payload = 'x'.repeat(Number(__ENV.PAYLOAD_BYTES || 1024));
export const options = {
  scenarios: {
    create: {executor:'constant-arrival-rate', exec:'create', rate:Math.ceil(rate/2), timeUnit:'1s', duration, preAllocatedVUs:vus, maxVUs:vus},
    read: {executor:'constant-arrival-rate', exec:'read', rate:Math.floor(rate/2), timeUnit:'1s', duration, preAllocatedVUs:vus, maxVUs:vus},
  },
  discardResponseBodies: true,
  systemTags: ['status','method','name','scenario','expected_response'],
  thresholds: {dropped_iterations:['count==0']},
  summaryTrendStats:['avg','med','p(95)','p(99)','max'],
};
export function setup() { console.log('LAB_LOAD_READY'); }
export function create() {
  const i = exec.scenario.iterationInTest;
  const r = http.post(base[i%2]+'/entities', JSON.stringify({id:run+'-'+i,payload}),
    {headers:{'Content-Type':'application/json'},timeout:'15s',tags:{name:'POST /entities'}});
  success.add(check(r, {'accepted':r=>r.status===202}), {operation:'create'});
}
export function read() {
  const i = exec.scenario.iterationInTest;
  const r = http.get(base[i%2]+'/entities/seed-'+((i*37)%1000),
    {timeout:'18s',tags:{name:'GET /entities/:id'}});
  success.add(check(r, {'found':r=>r.status===200}), {operation:'read'});
}
export function handleSummary(data) {
  return {'/results/summary.json': JSON.stringify(data,null,2),stdout:JSON.stringify({
    requests:data.metrics.http_reqs, failures:data.metrics.http_req_failed,
    dropped:data.metrics.dropped_iterations, latency:data.metrics.http_req_duration},null,2)+'\n'};
}
