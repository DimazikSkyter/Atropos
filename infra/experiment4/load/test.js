import http from 'k6/http';
import { check, fail, sleep } from 'k6';

const baseUrl = __ENV.BASE_URL || 'http://java:8080';
const positive = (name, fallback) => {
  const value = Number(__ENV[name] || fallback);
  if (!Number.isFinite(value) || value <= 0) {
    throw new Error(`${name} must be a positive number`);
  }
  return value;
};
const baseline = positive('BASELINE_SECONDS', 180);
const ramp = positive('RAMP_SECONDS', 900);
const hold = positive('HOLD_SECONDS', 180);
const duration = baseline + ramp + hold;
const rate = positive('LOAD_RPS', 250);

export const options = {
  scenarios: {
    constant_workload: {
      executor: 'constant-arrival-rate',
      rate,
      timeUnit: '1s',
      duration: `${duration}s`,
      preAllocatedVUs: 100,
      maxVUs: 500,
      gracefulStop: '5s',
    },
  },
  tags: { experiment: 'experiment4', service: 'java' },
  // The endpoint and labels are fixed; no per-request identifiers enter metrics.
  systemTags: ['status', 'method', 'name', 'scenario', 'expected_response'],
  thresholds: {
    'http_req_failed{scenario:constant_workload}': ['rate==0'],
    'checks{scenario:constant_workload}': ['rate==1'],
    dropped_iterations: ['count==0'],
  },
  setupTimeout: '60s',
  summaryTrendStats: ['avg', 'med', 'p(95)', 'p(99)', 'max'],
};

export function setup() {
  // Compose startup does not imply readiness. Wait before advancing experiment time.
  let healthy = false;
  for (let attempt = 0; attempt < 50; attempt++) {
    const response = http.get(`${baseUrl}/health`, {
      tags: { name: 'health', endpoint: 'health' }, timeout: '1s',
    });
    if (response.status === 200) {
      healthy = true;
      break;
    }
    sleep(1);
  }
  if (!healthy) fail('Java service did not become healthy in 50 seconds');
  const started = http.post(`${baseUrl}/start`, null, {
    tags: { name: 'start', endpoint: 'start' }, timeout: '5s',
  });
  if (started.status !== 200) {
    fail(`Experiment start failed: HTTP ${started.status}: ${started.body}`);
  }
  console.log(`Experiment4 started: ${baseline}s baseline + ${ramp}s ramp + ${hold}s hold; ${rate} RPS`);
}

export default function () {
  const response = http.get(`${baseUrl}/work`, {
    tags: { name: 'work', endpoint: 'work' },
    timeout: '5s',
  });
  check(response, { 'work returned 200': (r) => r.status === 200 });
}

export function handleSummary(data) {
  const summary = {
    experiment: 'experiment4',
    profile: { baseline_seconds: baseline, ramp_seconds: ramp, hold_seconds: hold, rate },
    ...data,
  };
  return {
    '/results/k6-summary.json': JSON.stringify(summary, null, 2),
    stdout: `Experiment4 completed. Raw request observations: /results/k6.ndjson.gz; summary: /results/k6-summary.json\n`,
  };
}
