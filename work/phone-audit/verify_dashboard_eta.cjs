// Offline DOM regression checks. No browser/network or phone mutation.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const path = require('node:path');
const root = path.resolve(__dirname, '../..');
const html = fs.readFileSync(path.join(root, 'web/progress.html'), 'utf8');
fs.mkdirSync(path.join(root, 'outputs/phone-video-compression-batches'), {recursive:true});
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
new vm.Script(script);
const elements = new Map();
for (const match of html.matchAll(/id="([^"]+)"/g)) {
  elements.set(match[1], { textContent: '', hidden: false, setAttribute() {}, removeAttribute() {}, replaceChildren() {}, append() {} });
}
let now = Date.now();
class TestDate extends Date { static now() { return now; } }
const context = vm.createContext({
  Date: TestDate, Intl, console,
  document: { getElementById(id) { assert(elements.has(id), 'Missing DOM element ' + id); return elements.get(id); }, querySelectorAll() { return []; }, createElement() { return {}; } },
  window: { setInterval() {} },
  fetch() { return new Promise(() => {}); },
});
vm.runInContext(script, context);
context.fixture = { status: 'running', updated_utc: new Date(now).toISOString(), eta: { suspended: false, phase: { remaining_seconds: 60, basis: 'live_progress' }, file: { low_seconds: 100, high_seconds: 300 }, global: { low_seconds: 3600, high_seconds: 7200, samples: 4 }, calibration_completed_files: 3 } };
vm.runInContext("data.worker=fixture; data.api={state:'running'}; drawETA()", context);
assert.match(elements.get('phase-eta').textContent, /1 min/);
assert.match(elements.get('overall-eta').textContent, /1 h à 2 h/);
now += 10_000;
vm.runInContext('drawETA()', context);
assert.match(elements.get('phase-eta').textContent, /50 s/);
vm.runInContext("data.worker.status='paused'; data.worker.eta.suspended=true; data.api.state='paused'; drawETA()", context);
const paused = elements.get('phase-eta').textContent;
now += 60_000;
vm.runInContext('drawETA()', context);
assert.equal(elements.get('phase-eta').textContent, paused);
assert.match(paused, /figée/);
vm.runInContext("data.worker={status:'running',updated_utc:new Date(Date.now()).toISOString(),eta:{calibration_completed_files:1}}; data.api.state='running'; drawETA()", context);
assert.match(elements.get('overall-eta').textContent, /1\/3/);
assert.match(elements.get('file-eta').textContent, /Calibration/);
vm.runInContext("data.worker.status='complete'; drawETA()", context);
assert.match(elements.get('overall-eta').textContent, /terminé/);
context.pipelineFixture = {
  updated_utc: new Date(now).toISOString(),
  phone_transfer: { phone_path: '/storage/emulated/0/DCIM/Camera/A.mp4', elapsed_seconds: 42, transaction_status: 'staging', eta_remaining_seconds: null },
  backup_prefetch: { phone_path: '/storage/emulated/0/DCIM/Camera/B.mp4', progress_percent: 37.5 },
};
vm.runInContext("data.worker=pipelineFixture; data.api.state='running'; drawPipeline()", context);
assert.equal(elements.get('phone-transfer-card').hidden, false);
assert.match(elements.get('phone-transfer-path').textContent, /A\.mp4$/);
assert.match(elements.get('phone-transfer-caption').textContent, /Envoi et contrôle.*45 s.*calibration/);
assert.equal(elements.get('prefetch-card').hidden, false);
assert.equal(elements.get('prefetch-progress').value, 37.5);
assert.match(elements.get('prefetch-caption').textContent, /37,5%.*vérifiée/);
vm.runInContext("data.worker.phone_transfer.eta_remaining_seconds=60; data.worker.phone_transfer.transaction_status='replaced_verifying'; drawPipeline()", context);
assert.match(elements.get('phone-transfer-caption').textContent, /galerie.*1 min/);
vm.runInContext('data.worker={}; drawPipeline()', context);
assert.equal(elements.get('phone-transfer-card').hidden, true);
assert.equal(elements.get('prefetch-card').hidden, true);
context.lanesFixture = {status:'running',parallel_encoding_enabled:true,pipeline_mode:'three_encoder_processes_serialized_phone',phase:'Encoding 1080p copy (nvenc)',workers:[
  {lane:'nvenc',status:'running',phase:'Encoding 1080p copy (nvenc)',current_file:'A.mp4',phase_progress_percent:20},
  {lane:'amf',status:'running',phase:'Encoding 1080p copy (amf)',current_file:'B.mp4',phase_progress_percent:40},
  {lane:'cpu',status:'running',phase:'Encoding 1080p copy (x265)',current_file:'C.mp4',phase_progress_percent:60},
]};
vm.runInContext('data.worker=lanesFixture; drawPipeline(); drawPhase()', context);
for (const [key,percent] of [['nvenc',20],['amf',40],['cpu',60]]) {
  assert.equal(elements.get('lane-'+key+'-card').hidden, false);
  assert.equal(elements.get('lane-'+key+'-progress').value, percent);
}
assert.equal(elements.get('phase-card').hidden, true);
assert.match(elements.get('parallel-mode').textContent, /simultanés activés/);
vm.runInContext('data.worker={}; drawPipeline()', context);
for (const key of ['nvenc','amf','cpu']) assert.equal(elements.get('lane-'+key+'-card').hidden, true);
for (const el of elements.values()) assert(!/NaN|undefined/.test(el.textContent));
const result = { checked_utc: new Date().toISOString(), syntax: true, dom_scope: 'offline simulated DOM, not a browser screenshot', live_countdown: true, paused_freeze: true, calibration_message: true, completion_message: true, pipeline_transfer_and_prefetch_cards: true, pipeline_eta_and_hide: true, no_nan: true };
fs.writeFileSync(path.join(root, 'outputs/phone-video-compression-batches/eta-dashboard-verification.json'), JSON.stringify(result, null, 2));
console.log(JSON.stringify(result));

context.photoFixture = {state:'running',completed:4,total:15,inventory_files:10509,records:[{original_bytes:1000,tests:[{method:'jpeg_q90',bytes:700}]}]};
vm.runInContext('data.photos=photoFixture;drawPhotos()', context);
assert.equal(elements.get('photos-card').hidden,false);
assert.match(elements.get('photos-summary').textContent,/4\/15/);
assert.match(elements.get('photos-results').textContent,/JPEG 90 : 30 %/);
vm.runInContext("data.photos.state='complete';drawPhotos()",context);
assert.match(elements.get('photos-status').textContent,/aucune photo remplacée/);
assert.equal(elements.get('photos-progress').hidden,true);
console.log(JSON.stringify({photo_progress:true,photo_gain_summary:true,photo_completion:true}));

context.photoPlanFixture={state:'awaiting_approval',saved_bytes:0,total_candidates:9494,prepared_pilot:4,pilot_total:4,excluded_known:3};
vm.runInContext('data.photoPlan=photoPlanFixture;drawPhotos()',context);
assert.match(elements.get('photos-status').textContent,/attente du feu vert/);
assert.equal(elements.get('photo-prepared').textContent,'4/4');
assert.match(elements.get('photos-summary').textContent,/aucun transfert photo lancé/);
assert.equal(elements.get('photos-progress').value,100);
console.log(JSON.stringify({separate_photo_section:true,approval_pending_visible:true,local_preparation_distinguished_from_phone_replacement:true}));

vm.runInContext('drawPhotos()',context);
assert.equal(elements.get('photo-overall-progress').value,0);
assert.equal(elements.get('photo-replaced').textContent,'0');
assert.match(elements.get('photo-overall-caption').textContent,/lot photo non démarré/);
vm.runInContext("data.photoPlan={...photoPlanFixture,state:'running',total_original_bytes:1000000000,processed_original_bytes:250000000,completed:10,phone_replacements:8};drawPhotos()",context);
assert.equal(elements.get('photo-overall-progress').value,25);
assert.equal(elements.get('photo-remaining').textContent,new Intl.NumberFormat('fr-CA',{maximumFractionDigits:0}).format(9484));
assert.equal(elements.get('photo-replaced').textContent,'8');
console.log(JSON.stringify({photo_global_byte_progress:true,pilot_copies_not_counted_as_replacements:true}));
