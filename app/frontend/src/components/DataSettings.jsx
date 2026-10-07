import { useEffect, useRef, useState } from 'react';
import { Database, MessageSquare, AlertTriangle, Check, Loader2 } from 'lucide-react';
import { api } from '../api/client.js';

function bytes(value) {
  if(value<1024)return `${value} bytes`;
  if(value<1024*1024)return `${(value/1024).toFixed(1)} KB`;
  if(value<1024*1024*1024)return `${(value/(1024*1024)).toFixed(1)} MB`;
  return `${(value/(1024*1024*1024)).toFixed(2)} GB`;
}
export function DataSettings({ onCleared, initialResult = null }) {
  const [preview,setPreview]=useState(null),[confirmation,setConfirmation]=useState('');
  const [busy,setBusy]=useState(''),[error,setError]=useState(''),[result,setResult]=useState(initialResult);
  const reviewHeading = useRef(null);
  useEffect(() => { if (preview) reviewHeading.current?.focus(); }, [preview]);
  async function review(scope) {
    setBusy('preview');setError('');setResult(null);setConfirmation('');setPreview(null);
    try{setPreview(await api.previewDataClear(scope));}catch(e){setError(e.message);}finally{setBusy('');}
  }
  async function clear() {
    setBusy('clearing');setError('');
    try{
      const response=await api.clearApplicationData(preview.token,confirmation);
      setResult(response);setPreview(null);setConfirmation('');
      // Session selection is app-owned UI state; appearance preferences stay.
      for(const key of Object.keys(sessionStorage))if(key.startsWith('salty-'))sessionStorage.removeItem(key);
      onCleared?.(response);
    }catch(e){setError(e.message);setPreview(null);}finally{setBusy('');}
  }
  return <div className="preferences-page data-settings">
    <h2>Data & storage</h2><p className="preferences-intro">Control what your assistant keeps.</p>
    {!result && <><section className="data-settings__choice"><MessageSquare size={18}/><div><h3>Delete chat sessions</h3><p>Remove all conversations, attachments, and their recorded outputs. Keep saved global memory and models.</p><button type="button" disabled={!!busy} onClick={()=>review('chats')}>Review chat data</button></div></section>
    <section className="data-settings__choice"><Database size={18}/><div><h3>Clear application data</h3><p>Remove chats, memories, generated files, managed datasets, logs, caches, and stored data backups. Keep the application, settings, runtimes, and model weights.</p><button type="button" disabled={!!busy} onClick={()=>review('all')}>Review all application data</button></div></section>
    <p className="data-settings__scope">Files outside app storage are included only when the app recorded creating them and they are unchanged. Unrelated and modified user files are preserved.</p></>}
    {busy?<p role="status" className="data-settings__status"><Loader2 size={15}/>{busy==='preview'?'Inspecting stored data…':'Clearing the reviewed data. Keep the app open…'}</p>:null}
    {error?<p role="alert" className="data-settings__error">{error}</p>:null}
    {preview?<section className="data-settings__review" aria-label="Review data deletion">
      <h3 ref={reviewHeading} tabIndex={-1}><AlertTriangle size={16}/>Review before deleting</h3>
      <dl><div><dt>Conversations</dt><dd>{preview.conversation_count}</dd></div><div><dt>Files</dt><dd>{preview.file_count.toLocaleString()}</dd></div><div><dt>File storage</dt><dd>{bytes(preview.bytes)}</dd></div>{preview.scope==='all'?<div><dt>Active memories</dt><dd>{preview.memory_count}</dd></div>:null}</dl>
      <p>Preserved: {preview.preserves.join('; ')}.</p>
      {preview.skipped_file_count>0?<p>{preview.skipped_file_count} linked or changed files will be left untouched.</p>:null}
      <p>This permanently removes the reviewed data. {preview.scope==='all'?'App-owned data backups are included.':''}</p>
      <label>Type <strong>{preview.confirmation}</strong> to confirm<input aria-label="Deletion confirmation" autoComplete="off" spellCheck={false} value={confirmation} disabled={!!busy} onChange={event=>setConfirmation(event.target.value)}/></label>
      <div className="data-settings__actions"><button type="button" disabled={!!busy} onClick={()=>setPreview(null)}>Cancel</button><button type="button" className="data-settings__delete" disabled={!!busy||confirmation!==preview.confirmation} onClick={clear}>Permanently delete reviewed data</button></div>
    </section>:null}
    {result?<section className="data-settings__result" role="status"><h3><Check size={16}/>{result.status==='completed'?'Data cleared':'Some cleanup remains'}</h3><p>{result.conversations_removed} conversations and {result.files_removed} files removed. Reclaimed {bytes(result.bytes_reclaimed)}.</p>{result.errors.map((value,i)=><p key={i}>{value}</p>)}<p>{result.restart_recommended?'Restart the app to reload its models and fresh workspace.':'Reload the workspace to refresh the conversation list.'}</p><button type="button" onClick={()=>window.location.reload()}>Reload workspace</button></section>:null}
  </div>;
}
