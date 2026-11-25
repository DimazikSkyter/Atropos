import http from 'k6/http';
import { check } from 'k6';
import exec from 'k6/execution';

const rate=Number(__ENV.RATE || 200), warm=Number(__ENV.WARM || 60);
const baseline=Number(__ENV.BASELINE || 90), diverse=Number(__ENV.DIVERSE || 120), recovery=Number(__ENV.RECOVERY || 90);
const seed=Number(__ENV.SEED || 20260930);
export const options={
  scenarios:{fixed:{executor:'constant-arrival-rate',rate,timeUnit:'1s',duration:`${warm+baseline+diverse+recovery}s`,preAllocatedVUs:100,maxVUs:100,gracefulStop:'20s'}},
  thresholds:{http_req_failed:['rate==0'],dropped_iterations:['count==0'],checks:['rate==1']},
  summaryTrendStats:['avg','p(95)','p(99)','max'],
};
// Stateless integer hash: the workload is independent of VU scheduling.
function uniform(i,salt) {
  let x=(i^seed^salt)>>>0; x=Math.imul(x^(x>>>16),0x7feb352d); x=Math.imul(x^(x>>>15),0x846ca68b);
  return (((x^(x>>>16))>>>0)+0.5)/4294967296;
}
const weights=Array.from({length:128},(_,i)=>Math.pow(1-uniform(i,991),-1/1.5));
const total=weights.reduce((a,b)=>a+b,0); let acc=0;
const cdf=weights.map(w=>(acc+=w/total));
export default function() {
  const i=exec.scenario.iterationInTest, t=i/rate;
  const phase=t<warm ? 0 : t<warm+baseline ? 1 : t<warm+baseline+diverse ? 2 : 3;
  // Exactly 80/20 over every five scheduled requests; the minor clients have Pareto weights.
  const minor=i%5===4;
  const u=uniform(i,1123);
  const client=minor ? 1+cdf.findIndex(x=>u<x) : 0;
  const entity=minor && phase===2 ? 1+Math.floor(uniform(i,3371)*4095) : 0;
  const response=http.post(`http://api${i%2+1}:8080/request`,JSON.stringify({entity,phase}),{
    headers:{'Content-Type':'application/json','X-Client-Id':String(client)},timeout:'15s',tags:{partition:minor?'1':'0',phase:String(phase)},
  });
  check(response,{'Kafka accepted':r=>r.status===202});
}
export function handleSummary(data) { return {'/results/summary.json':JSON.stringify(data,null,2)}; }
