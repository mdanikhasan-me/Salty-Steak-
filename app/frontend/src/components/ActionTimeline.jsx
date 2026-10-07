import { memo, useMemo, useState } from 'react';
import { Camera, Check, ChevronRight, FileCode2, Maximize2, Minus, Plus, Terminal, MousePointer2, RotateCcw } from 'lucide-react';
import { actionEvents, actionPresentation } from '../workflows/actionPresentation.mjs';
import { formatDuration } from '../workflows/formatters.js';
import { Dialog } from './Dialog.jsx';

function CapturePreview({item}) {
  const [open,setOpen]=useState(false), [failed,setFailed]=useState(false), [zoom,setZoom]=useState(1);
  if(!item.preview)return <p className="work-action__unavailable">No saved preview for this capture.</p>;
  if(failed)return <p className="work-action__unavailable">Preview unavailable <button type="button" onClick={()=>setFailed(false)}>Retry</button></p>;
  return <>
    <button type="button" className="capture-preview" onClick={()=>{setZoom(1);setOpen(true);}} aria-label={`Open ${(item.previewTitle || 'Screenshot').toLowerCase()} preview`}>
      <img src={item.preview} alt={item.label} loading="lazy" decoding="async" onError={()=>setFailed(true)}/>
      <span><Maximize2 size={13}/>View {(item.previewTitle || 'Screenshot').toLowerCase()}</span>
    </button>
    <Dialog open={open} onClose={()=>setOpen(false)} title={item.previewTitle || 'Screenshot'} description={item.width && item.height ? `${item.width} × ${item.height} · Saved during this action` : 'Saved during this action'} className="capture-viewer">
      <div className="capture-viewer__canvas"><img src={item.preview} alt="Saved screenshot" style={{width:`${zoom*100}%`,maxWidth:zoom===1?'100%':'none'}}/></div>
      <div className="capture-viewer__controls">
        <button type="button" aria-label="Zoom out" disabled={zoom<=.5} onClick={()=>setZoom(z=>Math.max(.5,z-.25))}><Minus size={16}/></button><span>{Math.round(zoom*100)}%</span>
        <button type="button" aria-label="Zoom in" disabled={zoom>=3} onClick={()=>setZoom(z=>Math.min(3,z+.25))}><Plus size={16}/></button>
        <button type="button" aria-label="Fit screenshot" onClick={()=>setZoom(1)}><RotateCcw size={15}/></button>
      </div>
    </Dialog>
  </>;
}
const ActionCard=memo(function ActionCard({event}) {
  const item=useMemo(()=>actionPresentation(event),[event]), [open,setOpen]=useState(false);
  const Icon=({image:Camera,file:FileCode2,terminal:Terminal})[item.kind] || MousePointer2;
  return <li className={`work-action work-action--${item.kind}`} data-state={item.state}>
    <button type="button" className="work-action__heading" onClick={()=>setOpen(v=>!v)} aria-expanded={open}>
      <ChevronRight size={13} className={open?'is-open':''}/><Icon size={15}/><span className="work-action__label">{item.label}</span>
      {item.change?<span className="work-action__counts"><ins>+{item.change.added}</ins><del>−{item.change.removed}</del></span>:null}
      {item.duration>0?<span className="work-action__time">{item.duration<100?'<0.1 sec':formatDuration(item.duration/1000)}</span>:null}
      {item.kind==='file'?<span className="work-action__view">{open?'Hide':'View'} {item.change?'changes':'details'}</span>:null}
    </button>
    {item.kind==='image'?<CapturePreview item={item}/>:null}
    {open?<div className="work-action__body">
      {item.path?<p className="work-action__path">{item.path}</p>:null}
      {item.input?<div className="work-action__command"><span>Command</span><pre>{item.input}</pre></div>:null}
      {item.change?.diff?<div className="work-action__diff" aria-label="Recorded file changes"><pre>{item.change.diff.split('\n').map((line,i)=><span key={i} data-line={line.startsWith('@@')?'range':line.startsWith('+')?'added':line.startsWith('-')?'removed':'context'}>{line || ' '}</span>)}</pre>{item.change.truncated?<small>Showing a bounded excerpt of this change.</small>:null}</div>
        :item.output?<pre className="work-action__output">{item.output}</pre>:null}
      {item.exitCode!==undefined && item.exitCode!==null?<p className="work-action__exit">{item.exitCode===0?<Check size={13}/>:null}Exit code {item.exitCode}</p>:null}
      <details className="work-action__technical"><summary>Action details</summary><pre>{item.details}</pre></details>
    </div>:null}
  </li>;
});
export function ActionTimeline({details={},live=false,workspaceMode='chat'}) {
  const events=useMemo(()=>actionEvents(details,live),[details,live]),[all,setAll]=useState(false);
  if(workspaceMode==='chat') {
    const image=details?.vision_input || details?.image_sha256 || details?.vision_provenance || events.some(e=>e.action==='screen.capture');
    return image?<p className="image-analysis-status"><Camera size={14}/>{live?'Analyzing the image':details.generation_state==='failed'?'Image analysis failed':'Analyzed the image'}</p>:null;
  }
  if(!events.length)return null;
  const visible=all?events:events.slice(-8);
  return <section className="work-timeline" aria-label={live?'Live actions':'Actions taken'}>
    {events.length>8?<button type="button" className="work-timeline__earlier" onClick={()=>setAll(v=>!v)}>{all?'Show recent actions':`Show ${events.length-8} earlier actions`}</button>:null}
    <ol>{visible.map((event,i)=><ActionCard key={`${event.observation?.audit_record_id || event.step || i}-${event.action}`} event={event}/>)}</ol>
  </section>;
}
