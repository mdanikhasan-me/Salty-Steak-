import test from 'node:test';
import assert from 'node:assert/strict';
import {liveProgress} from './liveProgress.mjs';
const op=(focus,tokens=100,id='one')=>({id,started_at:'2026-09-24T10:00:00Z',updated_at:'2026-09-24T10:03:00Z',result:{generation_preview:{kind:'reasoning',focus,token_count:tokens,tail_text:'private secret',summary:'All tests passed!'}}});
test('a long wait shows truthfully that answer text is not ready',()=>{
 const p=liveProgress(op('validation'));
 assert.equal(p.current.title,'Considering validation rules');
 assert.equal(p.reasoningOnly,true);
 assert.ok(!JSON.stringify(p).includes('private secret'));
 assert.ok(!JSON.stringify(p).includes('All tests passed'));
});
test('topic history is bounded and small changes do not flicker',()=>{
 let p=liveProgress(op('input',1));
 p=liveProgress(op('validation',2),p);assert.equal(p.current.key,'input');
 p=liveProgress(op('validation',50),p);assert.equal(p.current.key,'validation');
 assert.equal(p.history[0].key,'input');
 for(const [i,key] of ['edge_cases','checks','implementation','evidence','calculation'].entries())p=liveProgress(op(key,100+i*50),p);
 assert.equal(p.history.length,3);
 assert.equal(liveProgress(op('input',5,'two'),p).history.length,0);
});
test('output replaces reasoning status and unknown focus cannot inject a claim',()=>{
 assert.equal(liveProgress(op('All tests passed')).current.key,'reasoning');
 const value=op('validation');value.result.generation_preview.kind='output';
 const p=liveProgress(value);assert.equal(p.current.title,'Writing the answer');assert.equal(p.reasoningOnly,false);
});
test('phase changes and token resets bypass topic debounce',()=>{
 let p=liveProgress({id:'one',result:{}});
 p=liveProgress(op('input',1),p);assert.equal(p.current.key,'input');
 const writing=op('input',500);writing.result.generation_preview.kind='output';
 p=liveProgress(writing,p);assert.equal(p.current.key,'answer');
 p=liveProgress(op('validation',2),p);assert.equal(p.current.key,'validation');
 p=liveProgress(op('input',1),p);assert.equal(p.current.key,'input');
});
test('research failures do not appear to be reading sources',()=>{
 const p=liveProgress({id:'one',result:{research_progress:{phase:'search_failed'}}});
 assert.equal(p.current.title,'Source search unavailable');
 assert.match(p.body,/could not finish/);
});
test('live speed is shown only when it is measured',()=>{
 const value=op('validation');assert.equal(liveProgress(value).speed,null);
 value.result.generation_preview.decode_tokens_per_second=1.38;
 assert.equal(liveProgress(value).speed,1.38);
 value.result.generation_preview.decode_tokens_per_second=NaN;
 assert.equal(liveProgress(value).speed,null);
});
test('same-topic retry resets the debounce baseline',()=>{
 let p=liveProgress(op('input',1000));
 p=liveProgress(op('input',1),p);
 p=liveProgress(op('validation',50),p);
 assert.equal(p.current.key,'validation');
});
test('retry uses its own token count rather than the whole-turn count',()=>{
 const value=op('input',32768);value.result.generation_preview.stream_id='primary';
 let p=liveProgress(value);
 value.result.generation_preview={kind:'output',stream_id:'retry',token_count:33000,stream_token_count:232};
 p=liveProgress(value,p);assert.equal(p.tokens,232);assert.equal(p.current.key,'answer');
 assert.equal(p.current.sinceTokens,232);
});

test('focus notes show measured progress without invented work or private text',()=>{
 const value=op('checks');
 const p=liveProgress(value);
 assert.equal(p.focusNote.title,'Planning checks and examples');
 assert.equal(p.focusNote.description,'100 reasoning tokens generated');
 assert.ok(!JSON.stringify(p.focusNote).includes('private secret'));
 assert.ok(!JSON.stringify(p.focusNote).includes('passed'));
 value.result.generation_preview={kind:'output',token_count:150,stream_id:'answer'};
 const writing=liveProgress(value,p);
 assert.equal(writing.focusNote,null);
 assert.equal(liveProgress(op('reasoning')).focusNote.specific,false);
});

test('operation stage replaces generic preparation and stale output',()=>{
 const value={...op('validation'),phase:'Loading selected model'};
 value.result.generation_preview={};
 assert.equal(liveProgress(value).current.title,'Loading selected model');
 value.phase='Planning build and behavior checks';
 value.result.generation_preview={kind:'output',token_count:72};
 value.result.lock_in_verification={status:'planning',token_count:120};
 const p=liveProgress(value);
 assert.equal(p.current.title,value.phase);assert.equal(p.writing,false);
 assert.equal(p.tokens,120);assert.match(p.body,/120.*test-planning/);
});

test('each actual test command updates heading even within one stage',()=>{
 const value={id:'one',phase:'Running build and tests',result:{lock_in_verification:{status:'testing',name:'Compile main.cpp',index:1,total:2}}};
 let p=liveProgress(value);assert.equal(p.current.title,'Running: Compile main.cpp');
 value.result.lock_in_verification={status:'testing',name:'Boundary assertions',index:2,total:2};
 p=liveProgress(value,p);assert.equal(p.current.title,'Running: Boundary assertions');assert.equal(p.body,'Check 2 of 2');
});

test('elapsed time advances while active without fabricating a new stage',()=>{
 const value={id:'one',state:'running',phase:'Loading selected model',started_at:'2026-09-27T00:00:00Z',updated_at:'2026-09-27T00:00:05Z',result:{}};
 assert.equal(liveProgress(value,null,Date.parse('2026-09-27T00:00:19Z')).elapsed,19);
 value.state='completed';assert.equal(liveProgress(value,null,Date.parse('2026-09-27T00:00:19Z')).elapsed,5);
});
