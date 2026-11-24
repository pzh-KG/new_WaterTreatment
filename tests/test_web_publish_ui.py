"""Exercise the real batch and single-record click handlers with a local queue double."""
import re
import shutil
import subprocess
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "src/watertreatment/web_static/app.js"


def test_publication_click_selection_and_receipt():
    assert shutil.which("node"), "Node.js is required for the offline web UI tests"
    source = SCRIPT.read_text(encoding="utf-8")

    def entry(label):
        return re.search(r'button\("' + label + r'",[^\n]*?,"database"\)', source).group()

    latest = next(line for line in source.splitlines() if "function latestRecords(){" in line)
    dialog_code = source[source.index("  const publicationRequests"):source.index("  function reviews()")]
    harness = r"""
const assert = require('node:assert/strict');
const S = {page:'records',jobs:[],records:[]};
const record = (id, extra={}) => ({id, title:id, current:true, in_library:true, admissible:true, ...extra});
const click = () => new Event('click');
let dialog, resolvePost, failPost, requests=[], destination;
function el(tag, attrs={}, ...children) {
  return {tag, children, checked:false, ...attrs, isConnected:true, append(...items){children.push(...items)}};
}
const button = (label, onclick, cls) => el('button', {textContent:label, onclick, cls}, label);
function openModal(title, subtitle, body) {
  dialog = {body, open:true, close(){this.open=false;body.isConnected=false}};
  return dialog;
}
const numberTag = r => r.id;
const toast = () => {};
const loadBase = async () => {};
const perform = fn => fn();
const route = page => {destination=page};
const post = (path, payload) => {requests.push({path,payload});return new Promise((yes,no)=>{resolvePost=yes;failPost=no})};
const nodes = root => [root, ...(root?.children||[]).flat(Infinity).filter(Boolean).flatMap(nodes)];
const inputs = () => nodes(dialog.body).filter(n=>n.tag==='input');
const submit = () => nodes(dialog.body).find(n=>n.tag==='button'&&n.cls==='primary');
const hasText = text => nodes(dialog.body).some(n=>typeof n==='string' && n.includes(text));
"""
    cases = r"""
(async () => {
  const id='eligible-a';
  S.records=[record(id),record('eligible-b'),record('published',{published_current:true}),
    record('queued'),record('blocked',{admissible:false}),record('unknown',{admissible:undefined}),
    record('source-changed',{published_current:true,source_change:'new attachment'}),
    record('history',{current:false}),record('outside',{in_library:false})];
  S.jobs=[{kind:'publish',status:'queued',payload:{record_id:'queued'}}];
  const batch=BATCH, single=SINGLE;
  batch.onclick(click());
  assert(hasText('可同步 2 篇') && hasText('排队或同步中 1 篇') && hasText('尚未通过入库检查 3 篇'));
  assert.equal(requests.length,0);
  const [all,a,b]=inputs();
  assert.equal(inputs().length,3);
  assert(submit().disabled);
  all.checked=true; all.onchange();
  assert(a.checked && b.checked);
  b.checked=false; b.onchange();
  assert(all.indeterminate && !submit().disabled);
  const sending=submit().onclick(click());
  await submit().onclick(click());
  assert.deepEqual(requests,[{path:'/api/publish',payload:{record_ids:[id]}}]);
  assert(submit().disabled);
  failPost(new Error('模拟服务错误')); await sending;
  assert(!submit().disabled && dialog.open && a.checked);
  assert(nodes(dialog.body).some(n=>n.role==='status' && n.textContent.includes('模拟服务错误')));
  const retry=submit().onclick(click());
  resolvePost({job_ids:['synthetic-job']}); await retry;
  assert.equal(destination,'jobs'); assert(!dialog.open);
  single.onclick(click());
  assert.equal(inputs().length,1); assert(inputs()[0].checked);
  assert.equal(requests.length,2);
  S.records=S.records.map(r=>({...r,published_current:r.id.startsWith('eligible')||r.published_current}));
  batch.onclick(click());
  assert(hasText('当前没有可同步的文献') && hasText('最新版本已入库 3 篇'));
  assert.equal(inputs().length,0); assert.equal(submit(),undefined); assert.equal(requests.length,2);
})().catch(err=>{console.error(err);process.exitCode=1});
"""
    program = "\n".join([harness, latest, dialog_code,
                         cases.replace("BATCH", entry("批量同步入库")).replace("SINGLE", entry("同步到图谱"))])
    subprocess.run(["node", "-"], input=program, encoding="utf-8", check=True)
