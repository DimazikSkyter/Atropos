import http from 'k6/http';
import { Counter } from 'k6/metrics';

const accepted = new Counter('exp3_load_accepted');
const refused = new Counter('exp3_load_refused');
const payload = 'x'.repeat(Number(__ENV.PAYLOAD_BYTES || 4096));
export const options = {
  // Docker removes DNS registration when service1 exits. Retain its resolved IP
  // so failed TCP requests remain bounded by 100ms instead of waiting for DNS.
  dns: { ttl: 'inf', select: 'first', policy: 'onlyIPv4' },
  scenarios: { steady: { executor: 'constant-arrival-rate', rate: Number(__ENV.LOAD_RPS || 750),
    timeUnit: '1s', duration: '10m', preAllocatedVUs: 200, maxVUs: 400,
    gracefulStop: '2s' } },
  tags: { experiment: 'experiment3' },
  thresholds: { dropped_iterations: ['count==0'] },
};
export function setup() {
  const r = http.post('http://service2:8080/start', null);
  if (r.status !== 204) throw new Error(`Cannot start degradation timer: ${r.status}`);
}
export default function () {
  const r = http.post('http://service1:8080/submit', payload,
    { timeout: '100ms', tags: { name: 'submit' }, responseType: 'none' });
  if (r.status === 202) accepted.add(1); else refused.add(1);
}
