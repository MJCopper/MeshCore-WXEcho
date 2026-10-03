// Exercise the actual troubleshooting page script without browser dependencies.
// Run: node tests/test_replay_ui.cjs
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = fs.readFileSync('app/web/templates/_troubleshooting_dashboard.html', 'utf8');
const script = source.match(/<script>([\s\S]*?)<\/script>/)[1]
  .replace(/{{[\s\S]*?}}/g, '"Australia/Sydney"')
  .replace(/refreshDiagnostics\(\);refreshJob\(\);refreshLogs\(\);setInterval[\s\S]*$/, '');
class Element {
  constructor(){this.children=[];this.style={};this.textContent='';this.value='';}
  appendChild(child){this.children.push(child);}
  replaceChildren(){this.children=[];this.textContent='';}
}
const elements = new Map();
const context = vm.createContext({
  document: {
    getElementById(id){if(!elements.has(id))elements.set(id,new Element());return elements.get(id);},
    createElement(){return new Element();}
  }, Date, URLSearchParams, console
});
vm.runInContext(script, context);
const element = id => context.document.getElementById(id);
const preview = (dry_run=true,notices=1) => ({
  dry_run,notices,parts:notices*2,source:'rfs',mode:'resend',token:'reviewed',
  details:[],errors:[],exclusions:[]
});
async function main(){
  element('replay-source').value='rfs';
  element('replay-mode').value='resend';
  context.api=async()=>preview();
  await context.previewReplay();
  assert.match(element('start-replay').textContent,/simulation/);
  assert.match(element('replay-mode-warning').textContent,/No radio messages/);

  let replayCalls=0, release;
  context.api=(path)=>{
    if(path==='replay'){replayCalls++;return new Promise(resolve=>release=resolve);}
    return Promise.resolve({mode:'resend',source:'rfs',status:'completed',dry_run:true,
      processed:1,total:1,results:{'dry-run':1}});
  };
  const first=context.startReplay();
  await context.startReplay();
  assert.equal(replayCalls,1);
  release({});
  await first;
  assert.match(element('replay-job').textContent,/DRY RUN: no radio messages/);

  context.api=async()=>preview(false);
  await context.previewReplay();
  assert.match(element('start-replay').textContent,/live/);

  context.api=async()=>preview(true,0);
  await context.previewReplay();
  assert.equal(element('start-replay').style.display,'none');
  assert(element('replay-preview').children.some(c=>/Nothing to resend/.test(c.textContent)));

  let finishPreview;
  context.api=()=>new Promise(resolve=>finishPreview=resolve);
  const pending=context.previewReplay();
  context.invalidatePreview();
  finishPreview(preview());
  await pending;
  assert.equal(element('start-replay').style.display,'none');
  console.log('Replay UI checks passed: simulation, live mode, zero notices, duplicate clicks, stale previews.');
}
main().catch(error=>{console.error(error);process.exitCode=1;});
