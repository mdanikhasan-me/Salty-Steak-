import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import test from "node:test";
import { buildSync } from "esbuild";

const compiled = buildSync({
  stdin: { contents: `import React from 'react';
    import {renderToStaticMarkup} from 'react-dom/server';
    import {CookingActivityPanel} from './CookingActivityPanel.jsx';
    export function render(props) { return renderToStaticMarkup(<CookingActivityPanel {...props}/>); }`,
    resolveDir: fileURLToPath(new URL('.', import.meta.url)), loader: 'jsx' },
  bundle: true, platform: 'node', format: 'cjs', jsx: 'automatic', write: false,
}).outputFiles[0].text;
const module = { exports: {} };
new Function('require', 'module', 'exports', compiled)(createRequire(import.meta.url), module, module.exports);
const render = module.exports.render;

function metric(html, label, value) {
  assert.ok(html.includes(`<dt>${label}</dt><dd>${value}</dd>`), `${label} should display ${value}`);
}

test('Lock In metrics belong to the active pass, never the previous answer', () => {
  const operation={id:'one',result:{generation_preview:{kind:'output',token_count:700,decode_tokens_per_second:9},
    lock_in_verification:{status:'planning',token_count:83,decode_tokens_per_second:1.5}}};
  const html=render({active:true,operation});
  metric(html,'Test planning','83 tokens');metric(html,'Generation speed','1.50 tokens/s');
  assert.ok(!html.includes('9.00 tokens/s'));assert.ok(!html.includes('700 tokens'));
  operation.result.lock_in_verification={status:'testing',name:'Boundary assertions'};
  const testing=render({active:true,operation});assert.ok(!testing.includes('<dt>Generation speed</dt>'));
  assert.ok(!testing.includes('<dt>Output</dt>'));
});

test('saved answer separates answer output from total model tokens', () => {
  const html = render({message:{content:'Done',technical_details:{
    generated_output_tokens:1907,total_streamed_output_tokens:2163,
    generation_duration_seconds:1072,turn_duration_ms:1234265,decode_tokens_per_second:1.794767,
  }},operation:{result:{generation_preview:{state:'discarded',token_count:1907}}}});
  metric(html,'Output','1,907 tokens');metric(html,'Total generated','2,163 tokens');
  metric(html,'Generation speed','1.79 tokens/s');metric(html,'Elapsed','20 min');
  assert.ok(html.includes('Finished in 20 min'));
});

test('live metrics use current pass counts and recognize research previews', () => {
  for(const research of [false,true]) {
    const preview={kind:'output',stream_token_count:45,token_count:301,decode_tokens_per_second:1.8};
    const result=research?{research_progress:{generation_preview:preview}}:{generation_preview:preview};
    const html=render({active:true,operation:{id:'test',result}});
    metric(html,'Output','45 tokens');metric(html,'Total generated','301 tokens');
    metric(html,'Generation speed','1.80 tokens/s');
  }
});

test('reasoning and legacy totals are labelled without inventing answer counts', () => {
  const thinking=render({active:true,operation:{id:'test',result:{generation_preview:{kind:'reasoning',token_count:20,stream_token_count:20}}}});
  metric(thinking,'Reasoning','20 tokens');assert.ok(!thinking.includes('<dt>Total generated</dt>'));
  const legacy=render({message:{content:'Done',technical_details:{total_streamed_output_tokens:100}}});
  metric(legacy,'Total generated','100 tokens');assert.ok(!legacy.includes('<dt>Output</dt>'));
});
