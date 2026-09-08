import assert from "node:assert/strict";
import test from "node:test";

import { researchProgress, researchWaves, siteIcon } from "./researchProgress.mjs";

const RUNNING = {
  research_progress: {
    phase: "reading",
    waves: [
      {
        query: "2TB gen4 nvme ssd price bangladesh",
        state: "reading",
        opened: 3,
        verified: 1,
        sites: [
          { host: "www.startech.com.bd", url: "https://www.startech.com.bd/a", state: "read" },
          { host: "www.ryans.com", url: "https://www.ryans.com/b", state: "verified" },
          { host: "www.bdstall.com", url: "https://www.bdstall.com/c", state: "found" },
        ],
      },
    ],
  },
};

test("a wave names the sites it reached, while it is still reaching them", () => {
  const [wave] = researchWaves(RUNNING.research_progress);
  assert.equal(wave.label, "Searching 3 websites");
  assert.equal(wave.running, true);
  assert.equal(wave.detail, "1 validated");
  assert.deepEqual(
    wave.sites.map((site) => site.host),
    ["startech.com.bd", "ryans.com", "bdstall.com"],
  );

  assert.equal(wave.sites[1].state, "verified");
});

test("a finished wave says so in the past tense", () => {
  const [wave] = researchWaves({
    waves: [{ query: "q", state: "done", opened: 2, verified: 2, sites: [{ host: "a.com", url: "https://a.com/x" }] }],
  });
  assert.equal(wave.label, "Searched 1 website");
  assert.equal(wave.running, false);
});

test("two waves stay two waves", () => {


  const account = researchProgress({
    research_progress: {
      waves: [
        { query: "one", state: "done", sites: [{ host: "a.com", url: "https://a.com" }] },
        { query: "two", state: "searching", sites: [{ host: "b.com", url: "https://b.com" }] },
      ],
    },
  });
  assert.equal(account.waves.length, 2);
  assert.equal(account.siteCount, 2);
  assert.equal(account.running, true);
  assert.equal(account.summary, "Searched 2 websites");
});

test("a site read twice is one site", () => {
  const account = researchProgress({
    research_progress: {
      waves: [
        { query: "one", state: "done", sites: [{ host: "a.com", url: "https://a.com/1" }] },
        { query: "two", state: "done", sites: [{ host: "a.com", url: "https://a.com/2" }] },
      ],
    },
  });
  assert.equal(account.siteCount, 1);
});

test("search failure is distinguishable from completed empty research",()=>{
  const result=researchProgress({research_progress:{waves:[{state:'failed',error:'Provider unavailable',sites:[]}]}});
  assert.equal(result.failure,'Provider unavailable');assert.equal(result.running,false);
});

test("a successful retry retires an earlier Research failure headline",()=>{
  const failed={state:'failed',error:'Provider unavailable',sites:[]};
  for(const state of ['reading','done']){
    const result=researchProgress({research_progress:{waves:[failed,{state,opened:2,verified:1,sites:[{host:'a.com',url:'https://a.com'}]}]}});
    assert.equal(result.failure,'');assert.equal(result.running,state!=='done');assert.equal(result.opened,2);assert.equal(result.verified,1);
  }
});
test("a later search hit does not downgrade previously validated site evidence",()=>{
  const result=researchProgress({research_progress:{waves:[
    {state:'done',sites:[{host:'a.com',url:'https://a.com/source',state:'validated',title:'Read source'}]},
    {state:'searching',sites:[{host:'a.com',url:'https://a.com/new',state:'found'}]},
  ]}});
  assert.equal(result.sources.length,1);assert.equal(result.sources[0].state,'validated');assert.equal(result.sources[0].url,'https://a.com/source');
});

test("no research means nothing is rendered", () => {
  assert.equal(researchProgress({}), null);
  assert.equal(researchProgress(null), null);
  assert.equal(researchProgress({ research_progress: { waves: [] } }), null);
});

test("the icon comes from the site itself, and never from a scheme we cannot open", () => {
  assert.equal(siteIcon("https://www.ryans.com/a/b"), "https://www.ryans.com/favicon.ico");
  assert.equal(siteIcon("javascript:alert(1)"), "");
  assert.equal(siteIcon("not a url"), "");
});
