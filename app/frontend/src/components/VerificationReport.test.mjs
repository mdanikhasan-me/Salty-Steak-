import test from 'node:test';
import assert from 'node:assert/strict';
import {buildSync} from 'esbuild';
import {createRequire} from 'node:module';
import {fileURLToPath} from 'node:url';
const code=buildSync({stdin:{contents:`import React from 'react';import{renderToStaticMarkup}from'react-dom/server';import{VerificationReport}from'./VerificationReport.jsx';export function render(report){return renderToStaticMarkup(<VerificationReport report={report}/>);}`,resolveDir:fileURLToPath(new URL('.',import.meta.url)),loader:'jsx'},bundle:true,platform:'node',format:'cjs',jsx:'automatic',write:false}).outputFiles[0].text;
const module={exports:{}};new Function('require','module','exports',code)(createRequire(import.meta.url),module,module.exports);const render=module.exports.render;
test('verification shows actual attempts and escaped terminal output',()=>{
 const html=render({status:'passed',repairs:1,scope:'Explicit behavior checks',requirements:['add numbers'],covered_requirements:[0],attempts:[{status:'failed',checks:[{name:'addition',kind:'test',argv:['python','_checks/test.py'],exit_code:1,status:'completed',stderr:'<script>bad()</script>'}]},{status:'passed',checks:[{name:'addition',kind:'test',exit_code:0,status:'completed',stdout:'CHECKS_OK'}]}]});
 assert.ok(html.includes('Attempt 1'));assert.ok(html.includes('Attempt 2'));assert.ok(html.includes('CHECKS_OK'));assert.ok(html.includes('exit 1'));assert.ok(html.includes('&lt;script&gt;'));assert.ok(!html.includes('<script>'));assert.ok(html.includes('add numbers'));
});
test('unverified report never claims checks passed',()=>{const html=render({status:'unverified',issues:['Compiler unavailable']});assert.ok(html.includes('Not fully verified'));assert.ok(html.includes('Compiler unavailable'));assert.ok(!html.includes('Recorded checks passed'))});
