import { eventState, eventSummary, timelineFor } from './agentTimeline.mjs';
const short = value => String(value ?? '').slice(0, 24000);
export function actionEvents(details = {}, live = false) {
  const events = timelineFor(live ? {liveDetails:details} : {messageDetails:details});
  const fallback = details.agent_task?.steps || details.orchestration?.agent_task?.steps || [];
  const rows=(events.length ? events : fallback).filter(event => event?.action !== 'respond').slice(-80);
  if (details.vision_input_id && details.conversation_id && !rows.some(e=>e.action==='vision.analyze')) {
    rows.unshift({action:'vision.analyze',status:live?'running':details.generation_state==='failed'?'failed':'completed',
      observation:{vision_input_id:details.vision_input_id,conversation_id:details.conversation_id,
                   image_source:details.image_source}});
  }
  return rows;
}
export function actionPresentation(event) {
  const args = event.arguments || {}, observation = event.observation || {};
  const command = args.command || observation.command, state = eventState(event);
  const capture = event.action === 'screen.capture' || (event.action === 'browser.control' && command === 'capture_preview');
  const vision=event.action==='vision.analyze';
  const audit = observation.audit_record_id;
  const file = event.action === 'files.manage' || /^files\./.test(event.action || '');
  const write = file && (args.operation === 'write' || observation.operation === 'write');
  const changed = write && state === 'completed' && observation.readback_verified === true;
  const path = observation.change?.path || args.path || observation.affected_paths?.[0] || '';
  const name = path.split(/[\\/]/).at(-1), terminal = event.action === 'terminal.execute';
  let label = vision ? (state==='running'?'Analyzing the image':'Analyzed the image')
    : capture ? (observation.visual_analysis ? 'Analyzed a screenshot' : 'Captured the screen')
    : changed ? (observation.change?.created ? 'Created' : 'Edited') + ` ${name || 'a file'}`
    : write ? `Write ${name || 'file'}` : file ? `${args.operation === 'read' ? 'Read' : 'Inspected'} ${name || 'files'}`
    : terminal ? 'Ran a command' : eventSummary(event);
  if (state === 'failed') label = `Failed · ${label}`;
  if (state === 'waiting') label = `Needs approval · ${label}`;
  const input = terminal ? (Array.isArray(args.argv) ? args.argv.join(' ') : short(args.command)) : '';
  const output = terminal ? [short(observation.stdout?.text ?? observation.stdout), short(observation.stderr?.text ?? observation.stderr)].filter(Boolean).join('\n')
    : short(observation.visual_analysis || observation.summary || observation.content || observation.error || event.reason);
  return {label, state, kind:capture || vision ? 'image' : file ? 'file' : terminal ? 'terminal' : 'action',
    path,input,output,exitCode:observation.exit_code,previewTitle:vision?'Image':'Screenshot',
    preview:vision && observation.vision_input_id && observation.conversation_id
      ? `/api/chat/conversations/${encodeURIComponent(observation.conversation_id)}/vision-previews/${encodeURIComponent(observation.vision_input_id)}`
      :capture && audit && state==='completed' ? `/api/automation/previews/${encodeURIComponent(audit)}` : null,
    width:observation.image_width || observation.artifact?.width,height:observation.image_height || observation.artifact?.height,
    change:changed ? observation.change : null,duration:Number(event.duration_ms)||0,
    details:short(JSON.stringify({arguments:args,result:observation},null,2))};
}
