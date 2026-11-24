/* WaterTreatment local workbench. All scientific content is rendered as text. */
(() => {
  "use strict";
  const $ = (s, root = document) => root.querySelector(s);
  const S = {status: {}, papers: [], records: [], jobs: [], samples: [], answers: [], selected: new Set(), qaSelected: new Set(), details: new Map(), page: "overview", query: "", filter: "all", extractionFilter: "all", jobQuery: "", jobFilter: "all", recordQuery:"",recordFilter:"all",reviewFilter:"all",showHistory:false,activityRevision:null,workspaceReady:false,lastLegacyRefresh:0,review: new Map(), activeObservation: new Map(), currentEvidence: null, pollBusy: false, initialized: false};
  const nav = [["overview", "总览", "grid"], ["library", "文献库", "book"], ["jobs", "抽取任务", "layers"], ["review", "人工抽查", "check"], ["records", "数据与入库", "database"], ["qa", "文献问答", "chat"]];
  const icons = {grid:["M3 3h7v7H3z","M14 3h7v7h-7z","M3 14h7v7H3z","M14 14h7v7h-7z"],book:["M4 3h13a3 3 0 013 3v15H6a3 3 0 01-3-3V6a3 3 0 013-3","M3 17h17","M8 7h7","M8 11h5"],layers:["M12 3L2 8l10 5 10-5-10-5z","M2 12l10 5 10-5","M2 16l10 5 10-5"],check:["M20 11v8a2 2 0 01-2 2H5a2 2 0 01-2-2V6a2 2 0 012-2h9","M9 10l3 3 9-9"],database:["M20 6c0 2.2-3.6 4-8 4S4 8.2 4 6s3.6-4 8-4 8 1.8 8 4z","M4 6v12c0 2.2 3.6 4 8 4s8-1.8 8-4V6","M4 12c0 2.2 3.6 4 8 4s8-1.8 8-4"],chat:["M21 11.5a8.4 8.4 0 01-.9 3.8 8.5 8.5 0 01-7.6 4.7 8.4 8.4 0 01-3.8-.9L3 21l1.9-5.7a8.4 8.4 0 01-.9-3.8 8.5 8.5 0 014.7-7.6A8.4 8.4 0 0112.5 3h.5a8.5 8.5 0 018 8v.5z","M8 11h.01","M12 11h.01","M16 11h.01"],search:["M21 21l-5-5","M18 10a8 8 0 11-16 0 8 8 0 0116 0z"],plus:["M12 5v14","M5 12h14"],arrow:["M5 12h14","M13 6l6 6-6 6"],folder:["M3 7V5a2 2 0 012-2h5l2 3h7a2 2 0 012 2v11a2 2 0 01-2 2H5a2 2 0 01-2-2V7z"],file:["M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z","M14 2v6h6","M8 13h8","M8 17h5"],play:["M7 3l14 9-14 9V3z"],clock:["M22 12a10 10 0 11-20 0 10 10 0 0120 0z","M12 6v6l4 2"],spark:["M12 3l2.5 6.5L21 12l-6.5 2.5L12 21l-2.5-6.5L3 12l6.5-2.5L12 3z"],download:["M12 3v12","M7 10l5 5 5-5","M4 16v4h16v-4"],alert:["M12 3L1 21h22L12 3z","M12 9v5","M12 18h.01"],link:["M10 13a5 5 0 007 0l3-3a5 5 0 00-7-7l-2 2","M14 11a5 5 0 00-7 0l-3 3a5 5 0 007 7l2-2"]};
  const statusNames = {candidate_audited_with_limitations:"完成 · 有来源限制",candidate_model_audited:"自动审查完成",candidate_incomplete_budget:"预算内未完成",candidate_incomplete_reading_coverage:"阅读记录待补正",candidate_incomplete_failure:"抽取未完成",schema_valid_candidate:"候选已保存",not_run:"尚未执行",not_extracted:"尚未抽取",candidate_unverified:"候选记录",incomplete:"未完成",failed:"执行失败",interrupted:"执行中断",queued:"排队中",pending:"排队中",running:"处理中",completed:"已完成",succeeded:"已完成",cancelled:"已取消",not_started:"未处理",ready:"已识别",identified:"已识别",confirmed:"已确认",needs_confirmation:"待确认",needs_review:"待确认",ambiguous:"存在歧义",missing_main:"缺少正文",conflict:"存在冲突",unknown:"待确认",published:"已入库",loaded_as_candidate:"候选已入库",unpublished:"未入库",not_published:"未入库",not_reviewed:"未经人工抽查",unreviewed:"未经人工抽查",partially_reviewed:"部分内容已核查",partial_reviewed:"部分内容已核查",needs_reaudit:"需要回源复审",human_revised_candidate:"人工修订候选",reviewed:"已记录局部核查",draft:"草稿",superseded:"历史版本",source_changed:"来源已变化",needs_recheck:"需要回查",identity_conflict:"论文归属有冲突",source_unavailable:"来源不可用",source_unreadable:"内容读取待处理",unreadable:"无法读取内容",unavailable:"来源不可用",stale_source:"来源已变化",auto:"自动识别",registered:"已登记",duplicate:"重复附件",supported:"已有证据支持",unsupported:"证据不足",needs_revision:"需要修订"};
  const roleNames = {main_article:"正文",supporting_information:"SI",main_article_with_supporting_information:"正文 + SI 合并文件",other:"其他附件",unknown:"暂不确定",primary_final:"最终材料",final_component:"最终体系组分",optimized_variant:"最终材料变体",necessary_control:"必要对照",active_component:"活性组分",precursor:"前驱体",accessory:"辅助件",mechanism_only:"机理对照",unresolved:"身份待明确",development:"开发文献",heldout:"保留验证文献",unassigned:"普通文献",unspecified:"未指定",validation_reserved:"保留验证文献",validation:"验证文献"};
  const factNames = {reported:"已报告",estimated:"图上估读",not_reported:"原文未报告",not_available:"来源不可用",unread:"尚未核读",read_unquantified:"已读未量化",ambiguous:"对象不明确",conflict:"原文冲突",inferred:"模型推断",explicit_absence:"明确没有",not_applicable:"不适用"};
  const purposeNames = {coupled:"蒸发与催化联合实验",evaporation:"蒸发实验",catalysis:"催化实验",stability:"稳定性实验",characterization:"材料表征",mechanism:"机理实验",unresolved:"实验类型待明确"};
  const jointNames={J1:"装置、方法与同期测量支持同时运行",J2:"同一体系同期运行有依据，测量细节不完整",J3:"同一配方分别测试两种功能",J4:"仅有概念或系列层面的表述，关键条件可能不同",J5:"明确分阶段运行或仅有一种功能","J?":"现有证据不足以判断同时运行"};
  const pairNames={P1:"来自同一次测量或共同时间过程",P2:"同一报告条件组，未证明为同一实体重复",P3:"同一样品的独立功能测试",P4:"材料变体或关键条件不同","P?":"两项结果关系尚不明确"};
  const streamNames={feed:"进水",reaction_liquid:"反应液",treated_outlet:"处理后出水",concentrate:"浓缩液",condensate:"冷凝水",escaped_vapor:"逸出蒸汽",recirculation:"循环液",bypass:"旁路",other:"其他采样相",unknown:"采样相不明确"};
  const inputSourceNames={external:"外加",preloaded:"预先负载",in_situ:"原位产生",explicit_none:"作者明确未添加",not_reported:"来源未报告",conflict:"来源存在冲突",unknown:"来源待明确"};
  const origins = {reported_measurement:"作者报告",author_calculation:"作者计算",author_simulation:"作者模拟",author_hypothesis:"作者提出机制",plot_estimate:"图上估读",local_calculation:"本地计算",model_inference:"模型推断",unresolved:"来源待明确"};
  const analysisRoleNames={key_result:"关键结果",condition_comparison:"条件比较",stability:"稳定性",design_control:"设计对照",mechanistic_support:"机理支持",dense_series_point:"已归档曲线点",unclassified:"用途尚未分类"};
  const hasResearchScope=r=>["0.7","0.8","0.9"].includes(r?.rule_version)&&["research_core","series_digitization"].includes(r.extraction_scope);
  const visibleResearchObservations=(r,showArchived=false)=>(r.observations||[]).filter(o=>!hasResearchScope(r)||showArchived||o.analysis_role!=="dense_series_point");
  function el(tag, attrs = {}, ...children) {
    const n = document.createElement(tag);
    for (const [k,v] of Object.entries(attrs)) {
      if (v == null || v === false) continue;
      if (k === "class") n.className = v;
      else if (k === "text") n.textContent = v;
      else if (k.startsWith("on") && typeof v === "function") n.addEventListener(k.slice(2), v);
      else if (k === "checked" || k === "disabled" || k === "selected") n[k] = !!v;
      else if (k === "value") n.value = v;
      else n.setAttribute(k, String(v));
    }
    for (const c of children.flat(Infinity)) if (c != null && c !== false) n.append(c instanceof Node ? c : document.createTextNode(String(c)));
    return n;
  }
  function icon(name) { const n = document.createElementNS("http://www.w3.org/2000/svg", "svg"); for (const [k,v] of Object.entries({viewBox:"0 0 24 24",fill:"none",stroke:"currentColor","stroke-width":"1.7","stroke-linecap":"round","stroke-linejoin":"round","aria-hidden":"true"})) n.setAttribute(k,v); for (const d of icons[name] || icons.file) {const p = document.createElementNS(n.namespaceURI,"path");p.setAttribute("d",d);n.append(p);} return n; }
  const button = (text, action, cls = "", ico = null) => el("button", {type:"button",class:"button " + cls,onclick:action},ico ? icon(ico) : null,text);
  const fact = f => f && typeof f === "object" && "status" in f ? (f.value !== null && f.value !== undefined ? (Array.isArray(f.value) ? f.value.join("、") : String(f.value)) : factNames[f.status] || "未给出") : f === null || f === undefined || f === "" ? "—" : String(f);
  const isEstimated = o => o?.origin === "plot_estimate" || o?.value?.status === "estimated";
  const observationValueText = o => (isEstimated(o) && o?.value?.value != null ? "≈ " : "") + fact(o?.value);
  const normalizationEntries = o => [["value","数值"],["unit","单位"],["metric","指标"]]
    .filter(([key])=>o?.[key]?.normalized!==null&&o?.[key]?.normalized!==undefined)
    .map(([key,label])=>({key,label,text:fact({value:o[key].normalized,status:o[key].status}),reason:o[key].normalization_reason}));
  const observationNormalizationText = o => normalizationEntries(o).map(item=>`${item.label}：${item.text}${item.reason?"；说明："+item.reason:"；未登记换算说明"}`).join("；");
  function normalizationDetails(o){
    const entries=normalizationEntries(o);if(!entries.length)return document.createDocumentFragment();
    return el("details",{class:"details-section observation-normalization"},
      el("summary",{},"另存的规范化 / 换算候选"),
      el("p",{class:"small-text muted"},"上方保留源记录数值、单位与指标。以下换算另存，未认证为同一物理量，不直接替换源值，也不视为新增的作者明示值。全文审查完成不等于换算关系已确认。"),
      ...entries.map(item=>el("div",{class:"attachment-reason"},`${item.label}：${item.text}`,el("div",{class:"tiny muted"},item.reason||"未登记换算说明"))),
      entries.some(item=>item.key==="value")&&!entries.some(item=>item.key==="unit")?el("p",{class:"small-text muted"},"换算单位未单独登记，不能自动沿用源单位。是否只是数值约定改变，需结合原文核对。"):null);
  }
  const observationStatus = o => isEstimated(o) && o?.value?.value != null ? "estimated" : o?.value?.status;
  const observationCoordinates = o => (o?.coordinates || []).map(c => `${c.name}：${fact(c.fact)} ${fact(c.unit)}`).join("；");
  const hasSourceNumericPoint = o => typeof o?.value?.value === "number" && Number.isFinite(o.value.value)
    && ["reported", "estimated", "explicit_absence"].includes(o.value.status)
    && ["reported_measurement", "author_calculation", "author_simulation", "plot_estimate"].includes(o.origin)
    && !!o.value.evidence_ids?.length;
  function hasUsableCoordinates(o,dataKind=null){
    const coordinates=o?.coordinates||[];
    const number=value=>typeof value==="number"&&Number.isFinite(value);
    if(!coordinates.length||!coordinates.every(c=>["reported","estimated","explicit_absence"].includes(c.fact?.status)
      &&(number(c.fact?.value)||(typeof c.fact?.value==="string"&&c.fact.value.trim().length>0))))return false;
    const name=c=>String(c.name||"").trim().toLocaleLowerCase().replace(/[\s-]+/g,"_");
    if(dataKind==="time_series")return coordinates.some(c=>
      (["time","elapsed_time","reaction_time","irradiation_time","illumination_time","exposure_time","sampling_time","time_point","t"].includes(name(c))&&number(c.fact.value))
      ||(["clock_time","time_of_day","local_time"].includes(name(c))&&typeof c.fact.value==="string"&&/^(?:[01]?\d|2[0-3]):[0-5]\d(?::[0-5]\d)?$/.test(c.fact.value.trim())));
    if(dataKind==="cycle_series")return coordinates.some(c=>["cycle","cycle_index","cycle_number","cycle_count","cycle_no"].includes(name(c))&&number(c.fact.value));
    return true;
  }
  function estimateBadge(o){return isEstimated(o)&&o?.value?.value!=null?el("span",{class:"badge amber estimate-label",title:"由图表读取的近似值；不等于作者明确标注的数值，读取误差与实验误差分别记录。"},"图上估读"):null;}
  function evidenceUseText(o,eid){
    const fields={value:"数值",unit:"单位",material_id:"材料",experiment_id:"实验",time_window:"时间",coordinates:"坐标",metric:"指标",uncertainty:"实验误差",extraction_uncertainty:"估读误差"};
    const types={direct:"直接支持",context:"上下文",conflict:"冲突证据"};
    const links=(o.evidence_links||[]).filter(link=>link.evidence_id===eid);
    return links.length?links.map(link=>`${types[link.support_type]||link.support_type}：${(link.supported_fields||[]).map(name=>fields[name]||name).join("、")}；${link.explanation||""}`).join("；"):"此项尚未逐字段登记此证据支持的范围。";
  }
  function quantitativeFollowupCard(d){
    const items=d.audit?.quantitative_followups||[];
    if(!items.length)return document.createDocumentFragment();
    const names={pointwise_comparison:"逐点比较",kinetic_recalculation:"动力学重算",condition_comparison:"条件比较",cycle_comparison:"循环比较",storage_comparison:"储存比较",recalculation:"复算"};
    const coverage=new Map((d.record.quantitative_coverage||[]).map(item=>[item.id,item]));
    return el("details",{class:"details-section"},el("summary",{},`局部补抽清单（${items.length} 项）`),
      el("p",{class:"small-text muted"},"这些系列仍为部分抽取。下列比较和复算受限，已有结果仍须遵守各自的证据与来源限制。"),
      items.map(item=>el("div",{class:"attachment-card"},
        el("div",{class:"small-text"},(item.coverage_ids||[]).map(id=>coverage.get(id)?.label||id).join("；")),
        el("p",{class:"small-text"},item.recommended_action),
        el("div",{class:"tiny muted"},"受限用途："+(item.affected_query_types||[]).map(name=>names[name]||name).join("、")),
        el("div",{class:"tiny muted"},"来源："+(item.evidence||[]).map(anchor=>anchor.figure_or_table||`${anchor.source_id} / ${anchor.block_id}`).join("；")))));
  }
  function quantitativeCoverageCard(d){
    const entries=d.record.quantitative_coverage||[];
    const observations=new Map((d.record.observations||[]).map(o=>[o.id,o]));
    const assessment=d.quantitative_coverage_summary;
    const assessedRestrictions=new Map((assessment?.incomplete_series||[]).map(item=>[item.id,item]));
    const body=el("section",{class:"quantitative-coverage"},el("h3",{},"定量覆盖（模型登记，非完整性认证）"));
    if(!entries.length){body.append(el("p",{class:"inline-notice"},"未登记图表定量覆盖，已读页数不代表图表已完整抽取。需要逐条件比较、动力学拟合或复算时，应先补查相关图表。"));return body;}
    const states={complete:"清单项已结构化",partial:"部分抽取",unquantified:"已读未量化",unread:"未核读",unreadable:"无法辨读",not_available:"来源不可用",out_of_scope:"不在抽取范围"};
    const scopes={final_material:"最终材料",necessary_control:"必要对照",mechanism:"机理",external_benchmark:"外部文献基准",other:"其他"};
    body.append(el("p",{class:"small-text muted"},`已登记 ${entries.length} 项图表或数据系列。下列数量只描述这些清单项，不能据此推定全篇没有遗漏。`));
    for(const entry of entries){
      const count=new Set(entry.observation_ids||[]).size;
      const sourcePoints=[...new Set(entry.observation_ids||[])].map(id=>observations.get(id)).filter(hasSourceNumericPoint);
      const computed=assessedRestrictions.get(entry.id);
      const quantified=computed?.quantified_points??sourcePoints.length;
      const limits=assessment?[...(computed?.limitations||entry.limitations||[])]:[...(entry.limitations||[])];
      // The current backend supplies the canonical assessment. This fallback
      // lets an already-open older local server display new records cautiously.
      if(!assessment){
        if(entry.state==="complete"&&entry.expected_points==null)limits.push("来源中的预期数据点数尚未核定，不能据此认定逐点抽取完整。");
        else if(entry.state==="complete"&&quantified!==entry.expected_points)limits.push("已抽取的来源数值数量与预期点数不一致，完整性需要重新核查。");
        if(["time_series","cycle_series","condition_series","table_cells","scalar"].includes(entry.data_kind)&&quantified===0)limits.push("尚无带来源证据的独立数值点，不能认定此项已定量完成。");
        if(["time_series","cycle_series","condition_series","table_cells"].includes(entry.data_kind)&&sourcePoints.some(o=>!hasUsableCoordinates(o,entry.data_kind)))limits.push("部分来源数值尚无可用的时间、循环、条件或单元格坐标，不能直接逐点比较。");
      }
      const restricted=assessment?assessedRestrictions.has(entry.id):entry.state!=="out_of_scope"&&(entry.state!=="complete"||limits.some(x=>String(x).trim()));
      const pointCountMatched=computed?.point_count_matched??(entry.expected_points!=null&&quantified===entry.expected_points);
      const coordinatesAvailable=computed?.coordinates_available??(!["time_series","cycle_series","condition_series","table_cells"].includes(entry.data_kind)||sourcePoints.every(o=>hasUsableCoordinates(o,entry.data_kind)));
      const numericPointsAvailable=!["time_series","cycle_series","condition_series","table_cells","scalar"].includes(entry.data_kind)||quantified>0;
      const declaredLimits=(entry.limitations||[]).some(text=>String(text).trim());
      const analysisOnly=entry.state==="complete"&&restricted&&pointCountMatched&&coordinatesAvailable&&numericPointsAvailable&&declaredLimits;
      const label=entry.state==="complete"&&restricted?(analysisOnly?"点数已匹配 · 有分析限制":"完整性待确认"):states[entry.state]||entry.state;
      const item=el("details",{class:"quantitative-coverage-item"},el("summary",{},el("strong",{},entry.label||entry.id),el("span",{class:"badge "+(restricted?"amber":"")},label),el("span",{class:"tiny muted"},`关联 ${count} 条观测；来源数值 ${quantified} / 预期 ${entry.expected_points??"待核定"} 个数据单元`)),el("div",{class:"small-text muted"},`${scopes[entry.scope]||entry.scope} · ${blockLabel(entry.locator||{})||entry.block_id}`));
      if(entry.reason)item.append(el("p",{class:"small-text"},entry.reason));
      if(limits.length)item.append(el("ul",{class:"small-text"},[...new Set(limits)].map(x=>el("li",{},x))));
      body.append(item);
    }
    return body;
  }
  const shortDate = t => {if(!t)return "—";const d=new Date(t);return Number.isNaN(d.getTime())?String(t):d.toLocaleString("zh-CN",{month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit",hour12:false});};
  const safeUrl = u => {if(!u)return null;try{const p=new URL(u,location.origin);return p.origin===location.origin&&p.pathname.startsWith("/api/")?p.href:null;}catch{return null;}};
  const enc = encodeURIComponent;
  const recordPath = id => "/api/records/" + enc(id);
  function badge(value, custom) {const v=String(value||"unknown");const color=/fail|error|conflict/.test(v)?"red":/limitation|uncomplete|incomplete|needs|ambiguous|missing|interrupt|unreadable|unsupported/.test(v)?"amber":/complete|publish|succeed|ready|identified|confirmed|reviewed/.test(v)&&!/^un|not_/.test(v)?"green":/running|pending|queued/.test(v)?"blue":"";return el("span",{class:"badge "+(custom||color)},statusNames[v]||roleNames[v]||factNames[v]||v);}
  function toast(message, error=false){const n=el("div",{class:"toast"+(error?" error":""),role:"status"},String(message));$("#toast-region").append(n);setTimeout(()=>n.remove(),6500);}
  async function api(path, options={}) {const headers={Accept:"application/json",...(options.body?{"Content-Type":"application/json","X-WT-Token":S.status.csrf_token||""}:{}),...options.headers};let response;try{response=await fetch(path,{...options,headers,body:options.body?JSON.stringify(options.body):undefined});}catch{throw new Error("无法连接本地工作台服务。请确认服务仍在运行，再刷新页面。");}const type=response.headers.get("content-type")||"";const data=type.includes("json")?await response.json():{detail:await response.text()};if(!response.ok){let m=data.detail||data.error||"请求未完成";if(Array.isArray(m))m=m.map(x=>x.msg||JSON.stringify(x)).join("；");else if(typeof m==="object")m=JSON.stringify(m);const error=new Error(String(m));error.status=response.status;throw error;}return data;}
  const post=(path,body={})=>api(path,{method:"POST",body});
  async function perform(action, btn){if(btn)btn.disabled=true;try{return await action();}catch(e){toast(e.message,true);return null;}finally{if(btn)btn.disabled=false;}}
  function loading(message="正在读取数据…"){return el("div",{class:"initial-loading"},el("span",{class:"spinner"}),el("p",{},message));}
  function empty(title,description,action){return el("div",{class:"empty-state"},icon("book"),el("h3",{},title),el("p",{},description),action);}
  function errorPanel(error, retry){return el("div",{class:"error-state"},el("strong",{},"暂时无法显示内容"),el("p",{},error.message||error),retry?button("重新读取",retry,"small"):null);}
  function synchronizationNotice(){
    if(!S.workspaceReady)return el("div",{class:"compatibility-notice",role:"status"},"页面已更新，后台尚未加载统一状态接口。当前为兼容视图；重启后台后可同步桌面端续处理状态和统一统计。");
    const sync=S.status.sync_state;if(!sync||sync.state==="synced")return null;
    return el("div",{class:"compatibility-notice",role:"status"},sync.message||(sync.state==="syncing"?"正在同步已保存的运行结果；完成后将自动更新状态与统计。":"结果同步未完成，当前仍显示上次已登记结果。"));
  }
  function pageHeader(title,subtitle,actions=[],eyebrow="RESEARCH WORKSPACE"){return el("div",{class:"page-header"},el("div",{},el("div",{class:"eyebrow"},eyebrow),el("h1",{},title),el("p",{},subtitle),synchronizationNotice()),el("div",{class:"header-actions"},actions));}
  function card(title,body,extra){return el("section",{class:"card"},el("div",{class:"card-head"},el("h2",{},title),extra),body);}
  function field(label,input,help){const id=input.id||"field-"+Math.random().toString(36).slice(2);input.id=id;return el("div",{class:"form-field"},el("label",{for:id},label),input,help?el("div",{class:"help"},help):null);}
  function select(options,value,attrs={}){const n=el("select",{class:"field-select",...attrs});for(const [v,label] of options)n.append(el("option",{value:v,selected:v===value},label));return n;}
  function listParameters(page){
    if(page==="library")return {q:S.query,attachments:S.filter,extraction:S.extractionFilter};
    if(page==="records")return {q:S.recordQuery,filter:S.recordFilter,history:S.showHistory?"1":""};
    if(page==="jobs")return {q:S.jobQuery,filter:S.jobFilter};
    if(page==="review")return {filter:S.reviewFilter};
    return {};
  }
  function hashFor(page,id,parameters){
    const query=new URLSearchParams();
    for(const [key,value] of Object.entries(parameters||{}))if(value&&value!=="all")query.set(key,value);
    return page+(id?"/"+enc(id):"")+(query.size?"?"+query.toString():"");
  }
  function route(page,id,parameters){
    const target=hashFor(page,id,parameters??listParameters(page));
    if(location.hash==="#"+target)perform(renderPage);else location.hash=target;
  }
  function syncListLocation(page){history.replaceState(null,"","#"+hashFor(page,null,listParameters(page)));}
  function routeState(){
    const [path,query=""]=location.hash.replace(/^#/,"").split("?",2),parts=path.split("/");
    return {page:nav.some(x=>x[0]===parts[0])?parts[0]:"overview",id:parts[1]?decodeURIComponent(parts.slice(1).join("/")):null,params:new URLSearchParams(query)};
  }
  function openModal(title,subtitle,body){const d=$("#modal");$("#modal-content").replaceChildren(el("div",{class:"modal-head"},el("div",{},el("h2",{},title),el("p",{},subtitle)),el("button",{type:"button",class:"close-button","aria-label":"关闭",onclick:()=>d.close()},"×")),el("div",{class:"modal-body"},body));if(!d.open)d.showModal();return d;}
  function renderNav(){const n=$("#navigation");n.replaceChildren(...nav.map(([id,name,ico])=>{let count=id==="library"?S.papers.length:id==="jobs"?S.jobs.filter(j=>["running","queued","pending"].includes(j.status)).length:id==="review"?sampleRecordIds().size:0;return el("button",{type:"button",class:"nav-item"+(S.page===id?" active":""),"aria-current":S.page===id?"page":null,title:name,onclick:()=>route(id)},icon(ico),el("span",{},name),count?el("span",{class:"nav-count",title:id==="review"?`抽查清单中 ${count} 篇，不代表已完成核查`:null},id==="review"?`清单 ${count}`:count):null);}));$("#breadcrumb-page").textContent=(nav.find(x=>x[0]===S.page)||nav[0])[1];}
  function applyWorkspace(data){
    S.papers=data.papers||[];S.records=data.records||[];S.jobs=data.jobs||[];
    S.activityRevision=data.activity_revision??null;
    Object.assign(S.status,{stats:data.stats||{},metric_membership:data.metric_membership||{},activity_revision:S.activityRevision,paused:data.paused,sync_state:data.sync_state});
  }
  async function loadWorkspace(){
    try{const data=await api("/api/workspace");S.workspaceReady=true;return data;}
    catch(error){
      if(error.status!==404)throw error;
      S.workspaceReady=false;S.lastLegacyRefresh=Date.now();
      const [papersData,recordsData,jobsData]=await Promise.all([api("/api/papers"),api("/api/records"),api("/api/jobs")]);
      const papers=papersData.papers||[],registered=new Set(papers.map(p=>p.id));
      const records=(recordsData.records||[]).map(r=>({...r,in_library:registered.has(recordPaperId(r))}));
      const current=records.filter(r=>r.current!==false),ids=rows=>[...new Set(rows.map(recordPaperId).filter(id=>registered.has(id)))];
      const membership={papers:[...registered],records:ids(current),completed:ids(current.filter(r=>r.admissible===true)),
        published:ids(current.filter(r=>!r.source_change&&(r.published_current??["published","loaded_as_candidate"].includes(r.publish_status)))),
        reviewed:ids(current.filter(r=>["partially_reviewed","partial_reviewed","reviewed","needs_reaudit"].includes(r.review_status)))};
      return {papers,records,jobs:jobsData.jobs||[],paused:jobsData.paused,activity_revision:null,metric_membership:membership,
        stats:{...S.status.stats,...Object.fromEntries(Object.entries(membership).map(([key,value])=>[key,value.length]))}};
    }
  }
  function modelLabel(model, effort){
    if (!model) return "模型未记录";
    const sol = /^gpt-([0-9.]+)-sol$/.exec(model);
    return [sol ? sol[1] + " Sol" : model, effort ? String(effort).toUpperCase() : ""].filter(Boolean).join(" ");
  }
  function currentModelLabel(){ return modelLabel(S.status.model, S.status.effort); }
  function renderRuntimeStatus(){
    const model = $("#configured-model");
    if (model) model.textContent = currentModelLabel();
    updateQueueControl();
    const box=$("#runtime-banner");if(!box)return;
    const state=S.status.runtime_code;
    box.hidden=!!state&&!state.restart_required;
    if(box.hidden){box.replaceChildren();return;}
    box.replaceChildren(el("strong",{},state?"网页后台需要重启":"后台尚未提供启动版本记录"),
      el("p",{},state?.reason||"无法确认后台是否已加载当前代码，请重启网页后台后再开始新的模型任务。"),
      el("p",{},"历史结果和只读查阅仍保留。刷新浏览器不会重启后台；请在后台完成重启后刷新本页。"),
      button("查看运行版本",settingsModal,"small text-button"));
  }
  function updateQueueControl(){
    const control=$("#queue-toggle");if(!control)return;
    const modelQueued=S.jobs.some(j=>["extract","recover","recheck","ask"].includes(j.kind)&&["queued","pending"].includes(j.status));
    const needsRestart=!!(S.status.paused&&S.status.runtime_code?.restart_required&&modelQueued);
    control.disabled=needsRestart;
    control.textContent=needsRestart?"先重启后台再继续":S.status.paused?"继续任务队列":"暂停后续任务";
    control.title=needsRestart?"排队中的模型任务保持原状，请重启网页后台后继续。":"";
  }
  function runtimeVersionDetails(){
    const state=S.status.runtime_code;
    if(!state)return el("p",{class:"inline-notice"},"当前后台没有启动版本记录，不能凭页面已更新判断后台代码已加载。");
    const row=(label,value)=>[el("dt",{},label),el("dd",{},value)];
    return el("section",{},el("h3",{},"后台运行版本"),el("dl",{class:"runtime-version-list"},
      row("启动时应用代码树快照",el("code",{},state.startup_tree_digest||"未能建立稳定快照")),
      row("当前磁盘代码树",el("code",{},state.current_disk_digest||"当前无法核验")),
      row("启动快照时间 / 进程",`${state.started_at||"未知"} / PID ${state.pid??"未知"}`),
      row("模型任务状态",state.restart_required?"需要重启，暂不启动新模型任务":"启动快照与磁盘检查未发现变化")),
      el("p",{class:"tiny muted"},"这是实际应用目录在包启动时的代码树快照，不是 Python 内存代码的逐字节认证，也不代表文献数据已通过科学核验。"));
  }
  async function loadBase(){
    S.status=await api("/api/status");
    renderRuntimeStatus();
    const [workspace,samples,answers]=await Promise.all([loadWorkspace(),api("/api/samples"),api("/api/answers")]);
    applyWorkspace(workspace);S.samples=samples.samples||[];S.answers=answers.answers||[];
    $("#connection-dot").className="online-dot ready";$("#connection-label").textContent="本地服务已连接";
    S.samples.sort((a,b)=>String(b.created_at).localeCompare(String(a.created_at)));S.initialized=true;renderNav();
  }
  async function refresh(showToast=false){await perform(async()=>{await post("/api/refresh");S.details.clear();await loadBase();await renderPage();if(showToast){const sync=S.status.sync_state;toast(sync?.state==="syncing"?sync.message||"已请求刷新，结果同步中":sync?.state==="error"?sync.message||"结果同步未完成":"已刷新来源与结果索引",sync?.state==="error");}});}
  function recordSummary(id){return S.records.find(x=>x.id===id)||{};}
  async function detail(id,force=false){if(!force&&S.details.has(id))return S.details.get(id);const d=await api(recordPath(id));S.details.set(id,d);return d;}
  function sampleRecordIds(){return new Set(S.samples.flatMap(x=>x.record_ids||(x.sample&&x.sample.record_ids)||[]));}
  function latestRecords(){return S.records.filter(r=>r.current!==false&&r.in_library!==false);}
  function recordPaperId(r){return r.source_paper_id||r.paper_id;}
  function metricIncludes(metric,paperId){const ids=S.status.metric_membership?.[metric];return Array.isArray(ids)?ids.includes(paperId):false;}
  function metricCount(metric){const ids=S.status.metric_membership?.[metric];return Array.isArray(ids)?ids.length:S.status.stats?.[metric]??0;}
  function recordMatchesFilter(r,filter){
    if(filter==="all")return true;
    const current=r.current!==false;
    if(["completed","published","reviewed"].includes(filter))return current&&metricIncludes(filter,recordPaperId(r));
    return filter==="unfinished"&&current&&!metricIncludes("completed",recordPaperId(r));
  }
  function metricRoute(metric){
    if(metric==="papers")route("library",null,{});
    else if(metric==="completed")route("library",null,{extraction:"completed"});
    else if(metric==="reviewed")route("review",null,{filter:"reviewed"});
    else route("records",null,{filter:metric});
  }
  function literatureNumber(item){
    const paper=S.papers.find(p=>p.id===(item?.paper_id||item?.record?.paper?.id||item?.payload?.paper_id));
    return item?.folder_number??paper?.folder_number??null;
  }
  function numberTag(item){const n=literatureNumber(item);return el("span",{class:"literature-number",title:"原始文献文件夹的序号"},n==null?"序号未登记":`文献 ${n}`);}
  function stateBadge(state,label){const node=badge(state);if(label)node.textContent=label;return node;}
  function durationText(seconds){
    if(typeof seconds!=="number"||!Number.isFinite(seconds)||seconds<0)return "未记录";
    if(seconds<1)return "不足 1 秒";
    const n=Math.round(seconds),h=Math.floor(n/3600),m=Math.floor(n%3600/60),s=n%60;
    return [h?`${h} 小时`:null,m?`${m} 分`:null,s?`${s} 秒`:null].filter(Boolean).join(" ")||"0 秒";
  }
  function extractionInfo(p){
    if(p.extraction)return p.extraction;
    const r=S.records.find(r=>r.id===p.record_id)||latestRecords().find(r=>recordPaperId(r)===p.id);
    if(r)return {state:r.admissible?"completed":"incomplete",label:r.admissible?"已有抽取结果":"已有候选 · 完成状态待核对",summary:"当前后台尚未提供完整的任务摘要。",has_candidate:true,observation_count:r.observation_count,available_result:{record_id:r.id,status:r.status,label:statusNames[r.status]||r.status,observation_count:r.observation_count,admissible:r.admissible}};
    return {state:"unknown",label:"抽取状态待加载",summary:"请确认后台已加载最新页面规则。",has_candidate:false};
  }
  function extractionCell(p){
    const info=extractionInfo(p),available=info.available_result;
    const box=el("div",{class:"extraction-summary"},stateBadge(info.state,info.label),el("div",{class:"extraction-description"},info.summary||"暂无运行摘要"));
    if(info.has_candidate&&info.observation_count!=null)box.append(el("div",{class:"extraction-count"},`已保留 ${info.observation_count} 条候选观测`));
    if(available&&info.latest_run_id&&available.record_id!==info.latest_run_id)box.append(el("div",{class:"prior-result-note"},"当前已登记结果：",available.label||statusNames[available.status]||"候选结果",available.observation_count!=null?` · ${available.observation_count} 条观测`:""));
    const attempt=info.latest_attempt;
    if(attempt&&attempt.state!==info.state&&attempt.run_id!==available?.record_id)box.append(el("div",{class:"latest-attempt-note"},"最近一次运行：",attempt.label||statusNames[attempt.state]||"状态待核对"));
    return box;
  }
  function recordProcessingBadge(r){
    if(r.display_status)return stateBadge(r.display_status,r.display_status_label);
    if(r.admissible===false)return stateBadge("incomplete",r.status==="candidate_incomplete_reading_coverage"?"已抽取 · 阅读记录待补正":"已抽取 · 审查未完成");
    return badge(r.status||"candidate_unverified");
  }
  function recordRow(r,mode="records"){
    const current=r.current!==false;
    return el("tr",{},el("td",{class:"number-cell"},el("span",{class:"folder-number"},literatureNumber(r)??"—")),
      el("td",{},el("button",{type:"button",class:"table-title",onclick:()=>route(mode==="review"?"review":"records",r.id)},r.title||r.paper_id||"未命名文献"),el("div",{class:"table-subtitle"},r.doi||"未登记 DOI"),r.folder_name?el("div",{class:"table-subtitle"},r.folder_name):null),
      el("td",{},recordProcessingBadge(r),r.result_summary?el("div",{class:"table-subtitle"},r.result_summary):null),el("td",{},r.observation_count??"—"),el("td",{},badge(r.review_status||"not_reviewed")),
      el("td",{},badge(r.publish_status||"unpublished"),!current?el("div",{class:"tiny muted"},"历史版本"):null),
      el("td",{},button(mode==="review"?"开始核查":"查看结果",()=>route(mode==="review"?"review":"records",r.id),"small text-button")));
  }
  function recordsTable(records,mode="records"){
    if(!records.length)return empty(mode==="review"?"还没有入选抽查的文献":"还没有符合条件的抽取结果",mode==="review"?"生成少量抽查清单，或在结果页主动选择一篇。未入选文献无需逐篇审核。":"已有注册候选会显示在这里，未完成结果仍须遵守其审查限制。");
    return el("div",{class:"table-wrap"},el("table",{class:"data-table records-table"},el("thead",{},el("tr",{},["文献序号","文献","自动处理","观测数","人工核查","图谱状态","操作"].map(t=>el("th",{},t)))),el("tbody",{},records.map(r=>recordRow(r,mode)))));
  }
  function overview(){
    const stats=S.status.stats||{},current=latestRecords(),m=$("#main");
    m.replaceChildren(pageHeader("研究总览","从正文与补充材料中，建立可追溯的双功能材料证据。",[button("查看文献库",()=>route("library",null,{}),"","book"),button("扫描文献目录",scanModal,"primary","plus")],"EVIDENCE → MATERIALS"));
    const cards=[
      ["papers","已登记文献","正文与 SI 统一管理","book"],
      ["completed","自动处理完成","当前结果已完成自动审查与准入检查","check"],
      ["published","最新版本已入库","只统计当前结果对应的图谱版本","database"],
      ["reviewed","已人工抽查","当前结果已有实际核查记录","layers"]
    ];
    m.append(el("div",{class:"stats-grid"},cards.map(([key,title,description,ico])=>el("button",{type:"button",class:"stat-card stat-card-link",onclick:()=>metricRoute(key),"aria-label":`${title} ${metricCount(key)} 篇，查看对应文献`},
      el("span",{class:"stat-label"},title,icon(ico)),el("span",{class:"stat-value"},metricCount(key)),el("span",{class:"stat-detail"},description),el("span",{class:"stat-drilldown"},"查看文献",icon("arrow"))))));
    m.append(el("div",{class:"overview-facts"},
      el("span",{},`按文献计数 · 每篇使用一个当前结果`),
      button(`已有结果 ${metricCount("records")} 篇（含待处理候选）`,()=>route("records",null,{}),"small text-button"),
      button(`当前仍未完成任务 ${stats.failed_unresolved??S.jobs.filter(j=>!j.recovered&&["failed","interrupted","incomplete"].includes(j.status)).length} 项`,()=>route("jobs",null,{filter:"unfinished"}),"small text-button"),
      button(`后续已修复任务 ${stats.recovered_jobs??S.jobs.filter(j=>j.recovered).length} 项`,()=>route("jobs",null,{filter:"recovered"}),"small text-button")));
    const flow=card("文献处理流程",el("div",{},el("div",{class:"workflow-strip"},["附件识别","全文抽取","回查与修订","结果入库","证据问答"].map((t,i)=>el("div",{class:"workflow-step"},el("div",{class:"step-number"},String(i+1).padStart(2,"0")),el("div",{class:"step-label"},t)))),el("div",{class:"workflow-note"},"模型完成主要工作，人工抽查作为独立分支进行。")));
    const recent=card("最近的文献结果",recordsTable(current.slice(0,5)),button("全部结果 →",()=>route("records",null,{}),"small text-button"));
    const pending=S.papers.filter(p=>/needs|ambiguous|conflict|missing|unknown|unavailable|unreadable|unsupported/.test(p.attachment_status||"")).length;
    const actions=el("div",{class:"card-body"},actionRow("附件需要处理",pending?`${pending} 篇需要确认角色、归属或处理读取问题`:"当前没有待处理的附件",()=>route("library",null,{attachments:"needs_confirmation"}),"file"),actionRow("少量科研抽查",`${sampleRecordIds().size} 篇已进入抽查清单`,()=>route("review"),"check"),el("div",{class:"note-panel"},el("strong",{},"保留未知，也保留依据"),el("p",{},"自动审查完成与人工核查分别记录。当前版本发生变化时，旧版本的入库和核查不会自动计入当前版本。")));
    const recentJobs=el("div",{class:"card-body"},S.jobs.length?S.jobs.slice(0,3).map(j=>el("div",{class:"task-row"},el("div",{class:"task-icon"},icon("clock")),el("div",{class:"task-row-main"},el("h3",{},jobName(j)),el("p",{},shortDate(j.created_at))),jobDisplayBadge(j))):el("div",{class:"small-text muted"},"队列空闲。启动任务后，处理进度会显示在这里。"));
    m.append(el("div",{class:"dashboard-grid"},el("div",{},flow,el("div",{class:"section-gap"},recent)),el("div",{class:"side-stack"},card("需要关注",actions),el("div",{class:"section-gap"},card("后台任务",recentJobs,button("查看 →",()=>route("jobs"),"small text-button"))))));
  }
  function actionRow(title,text,action,ico){return el("div",{class:"task-row"},el("div",{class:"task-icon"},icon(ico)),el("div",{class:"task-row-main"},el("h3",{},title),el("p",{},text)),button("→",action,"small text-button"));}
  function library() {
    const main = $("#main");
    main.replaceChildren(pageHeader("文献库", "按原文件夹序号查看附件、抽取进展和已保留结果。", [
      button("扫描文献目录", scanModal, "", "folder"),
      button("抽取选中文献", () => extractionModal([...S.selected]), "primary", "play")
    ], "LITERATURE LIBRARY"));
    const list = el("div"), summary = el("div", {class:"filter-summary"});
    const search = el("input", {class:"search-input",type:"search",placeholder:"搜索标题、DOI 或文件夹序号",value:S.query,"aria-label":"搜索文献",oninput:e=>{S.query=e.target.value;syncListLocation("library");paint();}});
    const filter = select([["all","全部附件状态"],["ready","已识别"],["needs_confirmation","待确认 / 读取待处理"]],S.filter,{"aria-label":"附件状态筛选",onchange:e=>{S.filter=e.target.value;syncListLocation("library");paint();}});
    const extractionFilter=select([["all","全部抽取状态"],["not_started","尚未抽取"],["active","排队 / 处理中"],["completed","自动处理完成"],["unfinished","失败 / 未完成"],["has_candidate","已有候选数据"]],S.extractionFilter,{"aria-label":"抽取状态筛选",onchange:e=>{S.extractionFilter=e.target.value;syncListLocation("library");paint();}});
    const askSelected = () => {
      S.qaSelected = new Set(S.papers.filter(p=>S.selected.has(p.id)&&p.record_id).map(p=>p.record_id));
      if (!S.qaSelected.size) return toast("选中的文献还没有可供问答的抽取结果", true);
      route("qa");
    };
    const toolbar = el("div",{class:"toolbar"},el("div",{class:"search-control"},icon("search"),search),filter,extractionFilter,
      el("div",{class:"toolbar-right"},button("向选中文献提问",askSelected,"small"),button("清除选择",()=>{S.selected.clear();paint();},"small text-button")));
    main.append(el("section",{class:"card"},toolbar,summary,list));
    function paint() {
      const q = S.query.trim().toLocaleLowerCase();
      const rows = S.papers.filter(p=>!q||[p.title,p.doi,p.folder_name,p.folder_number].join(" ").toLocaleLowerCase().includes(q))
        .filter(p=>S.filter==="all"||(S.filter==="ready"?/ready|identified|confirmed/.test(p.attachment_status||""):/needs|ambiguous|unknown|conflict|missing|unavailable|unreadable|unsupported/.test(p.attachment_status||"")))
        .filter(p=>{const x=extractionInfo(p);return S.extractionFilter==="all"||(S.extractionFilter==="has_candidate"?x.has_candidate:S.extractionFilter==="completed"?metricIncludes("completed",p.id):S.extractionFilter==="active"?x.active===true||["queued","pending","running"].includes(x.latest_attempt?.state||x.state):S.extractionFilter==="unfinished"?["failed","incomplete","interrupted","cancelled"].includes(x.state):x.state===S.extractionFilter);});
      summary.replaceChildren(el("span",{},`显示 ${rows.length} / ${S.papers.length} 篇`),el("span",{},"·"),el("span",{class:"selection-count"},`已选 ${S.selected.size} 篇`));
      if (!rows.length) {
        list.replaceChildren(empty("这里还没有符合条件的文献",S.papers.length?"调整搜索词或筛选条件，不会改变原有文献数据。":"扫描已配置的文献目录，程序会识别文件与正文/SI角色。",button("扫描目录",scanModal,"small")));
        return;
      }
      const all = el("input",{type:"checkbox","aria-label":"选择当前筛选下的文献",checked:rows.every(p=>S.selected.has(p.id)),onchange:e=>{rows.forEach(p=>e.target.checked?S.selected.add(p.id):S.selected.delete(p.id));paint();}});
      const head = el("thead",{},el("tr",{},el("th",{class:"check-cell"},all),["序号","文献与来源文件夹","附件","识别状态","抽取状态与结果","操作"].map(t=>el("th",{},t))));
      const body = el("tbody");
      for (const p of rows) {
        const check = el("input",{type:"checkbox","aria-label":"选择 "+(p.title||p.folder_name),checked:S.selected.has(p.id),onchange:e=>{e.target.checked?S.selected.add(p.id):S.selected.delete(p.id);paint();}});
        const title = el("button",{type:"button",class:"table-title",onclick:()=>paperModal(p.id)},p.title||p.folder_name);
        const actions = el("div",{class:"table-actions"},button("查看附件",()=>paperModal(p.id),"small text-button"));
        const extraction=extractionInfo(p),resultId=extraction.available_result?.record_id||p.record_id;
        if(resultId)actions.append(button("查看结果",()=>route("records",resultId),"small text-button"));
        if(extraction.latest_job_id)actions.append(button("运行详情",()=>jobModal(extraction.latest_job_id),"small text-button"));
        const titleCell = el("td",{},title,el("div",{class:"table-subtitle"},p.doi||p.folder_name),el("div",{class:"badges",style:"margin-top:7px"},badge(p.cohort_role||"unassigned")));
        const filesCell = el("td",{},`${(p.files||[]).length} 份`,el("div",{class:"table-subtitle"},`${(p.files||[]).filter(f=>["main_article","main_article_with_supporting_information"].includes(f.role)).length} 正文 · ${(p.files||[]).filter(f=>f.role==="supporting_information").length} 独立 SI`),el("div",{class:"table-subtitle"},supportingInformationLabel(p)));
        const reviewSummary=attachmentReviewSummary(p);
        body.append(el("tr",{},el("td",{class:"check-cell"},check),el("td",{},el("span",{class:"folder-number"},p.folder_number??"—")),titleCell,filesCell,el("td",{},badge(p.attachment_status||"unknown"),reviewSummary?el("div",{class:"table-subtitle"},reviewSummary):null),el("td",{},extractionCell(p)),el("td",{},actions)));
      }
      list.replaceChildren(el("div",{class:"table-wrap library-table-wrap",tabindex:0,"aria-label":"文献列表，可滚动查看"},el("table",{class:"data-table library-table"},head,body)));
    }
    paint();
  }
  function scanModal(){
    const limit=500,chosen=new Set();
    let rootIndex=0,folders=[],busy=false,submitting=false,requestSerial=0;
    const rootChoice=select((S.status.source_roots||[]).map((path,index)=>[String(index),path]),"0",{"aria-label":"选择文献根目录"});
    const rootPath=el("div",{class:"scan-root-path mono"});
    const query=el("input",{class:"field-input",type:"search",placeholder:"名称、序号，或序号范围（例如 131-527）","aria-label":"搜索文献文件夹"});
    const onlyNew=el("input",{type:"checkbox"});
    const list=el("div",{class:"scan-folder-list","aria-label":"可选文献文件夹"});
    const loadErrors=el("div",{class:"scan-folder-errors"});
    const summary=el("div",{class:"scan-selection-summary","aria-live":"polite"});
    const selectionNotice=el("div",{class:"scan-selection-notice",role:"status"});
    const allVisible=el("input",{type:"checkbox","aria-label":"全选当前筛选结果"});
    const clear=button("清除已选",()=>{chosen.clear();selectionNotice.textContent="";draw();},"small text-button");
    const reload=button("重新读取目录",()=>loadFolders(rootIndex),"small text-button");
    const body=el("div",{class:"scan-picker"},
      el("div",{class:"note-panel scan-purpose"},el("strong",{},"选择根目录，再勾选需要扫描的文献文件夹"),
        el("p",{},"当前列表只读取文件名和附件数量。提交后才识别正文与 SI 并登记到文献库；不会启动模型抽取或写入 Neo4j。")),
      field("文献所在根目录",rootChoice),rootPath,
      el("div",{class:"scan-picker-toolbar"},query,el("label",{class:"scan-only-new"},onlyNew,"只看未登记")),
      el("div",{class:"scan-picker-selection"},el("label",{},allVisible,"全选当前筛选"),clear,reload),
      summary,selectionNotice,loadErrors,list);
    const d=openModal("选择文献文件夹","支持一次勾选多篇；无需改名或逐篇填写文件路径。",body);
    d.classList.add("scan-picker-dialog");
    d.addEventListener("close",()=>{requestSerial++;d.classList.remove("scan-picker-dialog");},{once:true});
    const run=button("扫描选中文献",submit,"primary","search");
    body.append(el("div",{class:"scan-picker-footer"},el("p",{class:"help"},"扫描登记后，在文献库选择抽取，并决定是否在自动审查完成后入图谱。单次最多 500 篇。"),
      el("div",{class:"form-actions"},button("取消",()=>d.close()),run)));
    const selectable=f=>typeof f.path==="string"&&f.file_count>0&&f.available!==false&&f.selectable!==false;
    function filtered(){
      const text=query.value.trim().toLocaleLowerCase();
      const range=text.match(/^(\d+)\s*[-—~～至]\s*(\d+)$/);
      return folders.filter(f=>{
        if(onlyNew.checked&&f.registered)return false;
        if(range)return f.folder_number!=null&&Number(f.folder_number)>=Number(range[1])&&Number(f.folder_number)<=Number(range[2]);
        return !text||String(f.name||"").toLocaleLowerCase().includes(text)||String(f.folder_number??"").includes(text);
      });
    }
    function updateSelection(visible){
      const selectableRows=visible.filter(selectable),visibleSelected=selectableRows.filter(f=>chosen.has(f.path)).length;
      const selectedFiles=folders.filter(f=>chosen.has(f.path)).reduce((count,f)=>count+Number(f.file_count||0),0);
      const hidden=chosen.size-visible.filter(f=>chosen.has(f.path)).length;
      summary.textContent=`当前显示 ${visible.length} / ${folders.length} 个文件夹 · 已选 ${chosen.size} 篇、${selectedFiles} 份附件${hidden?`（其中 ${hidden} 篇不在当前筛选内）`:""}`;
      allVisible.checked=selectableRows.length>0&&visibleSelected===selectableRows.length;
      allVisible.indeterminate=visibleSelected>0&&visibleSelected<selectableRows.length;
      allVisible.disabled=busy||submitting||!selectableRows.length;
      clear.disabled=busy||submitting||!chosen.size;
      reload.disabled=busy||submitting;
      rootChoice.disabled=busy||submitting||!rootChoice.options.length;
      query.disabled=busy||submitting;
      onlyNew.disabled=busy||submitting;
      run.disabled=busy||submitting||!chosen.size||chosen.size>limit;
      run.textContent=submitting?"正在提交扫描…":`扫描选中文献${chosen.size?`（${chosen.size} 篇）`:""}`;
    }
    function draw(){
      const visible=filtered();
      updateSelection(visible);
      if(busy){list.replaceChildren(loading("正在列出文献文件夹与附件数量…"));return;}
      if(!visible.length){list.replaceChildren(empty(folders.length?"没有匹配的文件夹":"目录下暂无可选文献文件夹",folders.length?"可调整名称、序号范围或取消“只看未登记”。":"请选择可用的文献根目录；文件夹需直接位于该根目录下。"));return;}
      const rows=visible.map(f=>{
        const enabled=selectable(f),picked=chosen.has(f.path);
        const check=el("input",{type:"checkbox",checked:picked,disabled:!enabled||submitting,"aria-label":"选择 "+f.name,onchange:event=>{
          if(event.target.checked&&chosen.size>=limit){event.target.checked=false;selectionNotice.textContent="已达到单次 500 篇上限。请减少已选项，或分次扫描；没有自动截取或遗漏选择。";return;}
          event.target.checked?chosen.add(f.path):chosen.delete(f.path);
          selectionNotice.textContent="";
          event.target.closest("tr").classList.toggle("is-selected",event.target.checked);
          updateSelection(filtered());
        }});
        check.id="scan-folder-"+Math.random().toString(36).slice(2);
        const state=f.record_id?"已有抽取结果":f.registered?"已登记":"尚未登记";
        return el("tr",{class:(picked?"is-selected ":"")+(enabled?"":"is-unavailable")},
          el("td",{},check),el("td",{class:"scan-folder-number"},f.folder_number??"—"),
          el("td",{},el("label",{class:"scan-folder-name",for:check.id},f.name),
            !enabled?el("div",{class:"table-subtitle"},f.reason||f.message||(f.file_count>0?"此文件夹不可选择":"未发现支持的 PDF / DOCX / DOC 附件")):null),
          el("td",{},el("span",{},`${f.file_count||0} 份`),el("div",{class:"table-subtitle"},(f.formats||[]).map(format=>String(format).replace(/^\./,"").toUpperCase()).join(" · ")||"—")),
          el("td",{},el("span",{class:"badge "+(f.record_id?"green":f.registered?"blue":"")},state),
            f.registered&&f.attachment_status?el("div",{class:"table-subtitle"},statusNames[f.attachment_status]||f.attachment_status):null));
      });
      list.replaceChildren(el("table",{class:"data-table scan-folder-table"},el("thead",{},el("tr",{},["选择","序号","文献文件夹","附件","登记状态"].map(label=>el("th",{scope:"col"},label)))),el("tbody",{},rows)));
    }
    allVisible.addEventListener("change",()=>{
      const visible=filtered().filter(selectable);
      if(allVisible.checked){
        const additions=visible.filter(f=>!chosen.has(f.path));
        if(chosen.size+additions.length>limit){selectionNotice.textContent=`本次全选将达到 ${chosen.size+additions.length} 篇，超过单次 500 篇上限。请用序号范围缩小列表，或逐项选择；当前选择保持不变。`;draw();return;}
        visible.forEach(f=>chosen.add(f.path));
      }else visible.forEach(f=>chosen.delete(f.path));
      selectionNotice.textContent="";draw();
    });
    query.addEventListener("input",()=>{selectionNotice.textContent="";draw();});
    onlyNew.addEventListener("change",()=>{selectionNotice.textContent="";draw();});
    rootChoice.addEventListener("change",()=>{chosen.clear();query.value="";selectionNotice.textContent="";loadFolders(Number(rootChoice.value));});
    async function loadFolders(index){
      const ticket=++requestSerial;
      rootIndex=index;busy=true;folders=[];loadErrors.replaceChildren();draw();
      try{
        const data=await api("/api/scan-folders?root_index="+enc(index));
        if(ticket!==requestSerial||!body.isConnected)return;
        rootIndex=data.root_index;
        rootChoice.replaceChildren(...(data.roots||[]).map(root=>el("option",{value:String(root.index),selected:root.index===rootIndex},root.path+(root.available?"":"（当前不可用）"))));
        rootPath.textContent=data.root_path||"尚未配置文献根目录";
        folders=data.folders||[];
        const valid=new Set(folders.filter(selectable).map(f=>f.path));
        for(const path of chosen)if(!valid.has(path))chosen.delete(path);
        if(data.errors?.length)loadErrors.append(el("details",{class:"details-section"},el("summary",{},`${data.errors.length} 处目录读取问题`),el("ul",{},data.errors.map(error=>el("li",{},error.path?el("span",{class:"mono"},error.path+"："):null,error.message||"目录不可读取")))));
        busy=false;draw();
      }catch(error){
        if(ticket!==requestSerial||!body.isConnected)return;
        busy=false;updateSelection([]);run.disabled=true;list.replaceChildren(errorPanel(error,()=>loadFolders(index)));
      }
    }
    async function submit(){
      if(busy||submitting||chosen.size===0||chosen.size>limit)return;
      const paths=folders.filter(f=>chosen.has(f.path)&&selectable(f)).map(f=>f.path);
      if(paths.length!==chosen.size){toast("部分已选文件夹当前不可用，请重新读取目录后选择。",true);return;}
      submitting=true;draw();
      try{
        await post("/api/scan",{folders:paths});
        if(body.isConnected)d.close();
        toast(`已提交 ${paths.length} 篇的扫描登记任务；未启动全文抽取或图谱写入。`);
        await loadBase();route("jobs");
      }catch(error){toast(error.message,true);}
      finally{submitting=false;if(body.isConnected)draw();}
    }
    loadFolders(0);
  }
  async function paperModal(id) {
    const d=openModal("文献附件","正在读取论文身份和附件判定…",loading());
    try {
      const p={...(S.papers.find(x=>x.id===id)||{}),...await api("/api/papers/"+enc(id))};
      const body=el("div",{},el("dl",{class:"info-list"},
        el("dt",{},"文献序号"),el("dd",{},numberTag({...p,paper_id:p.id})),
        el("dt",{},"文件夹"),el("dd",{class:"mono"},p.folder),
        el("dt",{},"DOI"),el("dd",{},p.doi||"尚未识别"),
        el("dt",{},"识别状态"),el("dd",{},badge(p.attachment_status||"unknown")),
        el("dt",{},"补充材料"),el("dd",{},supportingInformationLabel(p),supportingInformationDetails(p))));
      body.append(el("section",{class:"paper-extraction-summary"},el("h3",{},"抽取进展与结果"),extractionCell(p)));
      const reviewNotice=attachmentReviewNotice(p);
      if(reviewNotice)body.append(reviewNotice);
      for(const f of p.files||[]) {
        const opts=Object.entries(roleNames).filter(([k])=>["main_article","supporting_information","main_article_with_supporting_information","other","unknown"].includes(k));
        const role=select(opts,f.role||"unknown");
        const note=el("input",{class:"field-input",placeholder:"确认依据或备注（可选）","aria-label":"附件确认备注"});
        const confirm=button("确认角色",()=>perform(async()=>{
          await post("/api/papers/"+enc(id)+"/roles",{file_id:f.id,role:role.value,reviewer:reviewerName(),note:note.value});
          toast("附件角色已登记，原文件名保持不变");S.details.clear();await loadBase();await paperModal(id);
          if(S.page==="library")library();
        },confirm),"small");
        const a=el("div",{class:"attachment-card"},
          el("div",{class:"attachment-head"},el("div",{},el("div",{class:"attachment-name"},f.name),
            el("span",{class:"tiny muted"},String(f.format||"").toUpperCase())),attachmentStatusBadge(f)),
          el("div",{class:"tiny muted"},attachmentOriginText(f)),
          el("div",{class:"attachment-reason"},(f.reasons||[]).join("；")||"未提供判定依据"));
        const readability=attachmentReadabilityText(f);
        if(readability)a.append(el("div",{class:"inline-notice"},readability));
        if(f.role_origin==="codex_attachment_review"&&typeof f.attachment_review?.reason==="string")
          a.append(el("div",{class:"attachment-reason"},"原文核对依据："+f.attachment_review.reason));
        a.append(el("div",{class:"attachment-actions"},role,confirm),el("div",{style:"margin-top:8px"},note));
        if(f.preview_text)a.append(el("details",{class:"details-section"},el("summary",{},"查看识别内容（抽样，非全文阅读）"),el("p",{class:"wrappable",style:"white-space:pre-wrap"},f.preview_text)));
        body.append(a);
      }
      const extract=button("抽取这篇文献",()=>{d.close();extractionModal([p.id]);},"primary","play");
      extract.disabled=p.attachment_status!=="ready";
      if(extract.disabled)extract.title="先处理附件读取、角色或归属问题";
      body.append(el("div",{class:"form-actions"},p.record_id?button("查看抽取结果",()=>{d.close();route("records",p.record_id);}):null,extract));
      openModal(p.title||p.folder_name,"正文、SI与附件归属分别登记；确认角色不等于科研内容已经核查。",body);
    }catch(e){$("#modal-content .modal-body").replaceChildren(errorPanel(e));}
  }
  function supportingInformationLabel(paper) {
    return ({present:"已识别 SI",author_not_provided:"作者未提供 SI · 用户已核实",
      not_obtained_or_not_identified:"SI 尚未取得或识别"})[paper.supporting_information_state]||"SI 状态尚未登记";
  }
  function supportingInformationDetails(paper) {
    if(paper.supporting_information_state!=="author_not_provided")return null;
    const proof=paper.supporting_information_verification||{};
    const attribution=[proof.confirmed_by?`核实人：${proof.confirmed_by}`:"依据用户对本批文献的核实",
      proof.confirmed_at?`登记时间：${shortDate(proof.confirmed_at)}`:null].filter(Boolean).join(" · ");
    return el("div",{class:"small-text muted"},attribution,
      proof.statement?el("div",{},proof.statement):null,
      el("div",{},"这是补充材料提供情况的记录；具体条件是否未报告，仍需结合正文判断。"));
  }
  function attachmentStatusBadge(file) {
    return file.status==="unsupported"
      ?el("span",{class:"badge amber"},"格式暂不支持读取")
      :badge(file.status||file.role||"unknown");
  }
  function attachmentOriginText(file) {
    const origin=({single_document_rule:"单文件正文规则",human:"人工确认文件角色",registered:"沿用已登记文件角色",codex_attachment_review:"Codex原文核对（附件）"})[file.role_origin]||"根据文件名与抽样内容识别";
    const language=({zh:"抽样含中文",en:"抽样含英文",mixed:"抽样含中英文"})[file.signals?.language_hint];
    return origin+(language?" · "+language:"");
  }
  function attachmentReviewSummary(paper) {
    const review=paper.attachment_review_details;
    if(!review)return "";
    if(review.state==="expired")return "附件核对已失效 · 需重新核对";
    if(review.state!=="applied")return "";
    const count=Array.isArray(review.limitations)?review.limitations.filter(x=>typeof x==="string"&&x.trim()).length:0;
    return "Codex已核对附件"+(count?` · ${count} 项核对说明`:"");
  }
  function attachmentReviewNotice(paper) {
    const review=paper.attachment_review_details;
    if(!review)return null;
    if(review.state==="expired")return el("div",{class:"inline-notice"},
      el("strong",{},"原附件核对已失效"),
      el("p",{},"来源文件或核对依据已变化，需重新核对角色与论文归属。历史核对记录保留。"));
    if(review.state!=="applied")return null;
    const limits=Array.isArray(review.limitations)?review.limitations.filter(x=>typeof x==="string"&&x.trim()):[];
    return el("div",{class:limits.length?"inline-notice":"note-panel"},
      el("strong",{},"Codex原文核对（附件）"),
      el("p",{},"已核对文件角色与论文归属；这不代表人工审核或科学数据验证。"),
      review.reviewed_at?el("div",{class:"tiny muted"},"核对时间："+shortDate(review.reviewed_at)):null,
      limits.length?el("div",{},el("strong",{},"核对范围与来源限制"),el("ul",{},limits.map(item=>el("li",{},item))),
        el("p",{class:"small-text"},"本地未定位到的内容保留为来源限制，不据此认定作者未提供或未报告。")):null);
  }
  function attachmentReadabilityText(file) {
    const state=file.readability||file.status;
    if(!["unreadable","unsupported"].includes(state))return null;
    const known=["main_article","main_article_with_supporting_information","supporting_information"].includes(file.role);
    return (known?`已确定为${roleNames[file.role]}，`:"")+
      (state==="unsupported"?"此格式的内容读取仍需处理。":"内容暂时无法读取。")+
      (known?"文件角色无需重复确认；读取问题解决前不能开始抽取。":"这不代表作者未报告科研信息。");
  }
  function reviewerName(){return localStorage.getItem("wt.reviewer")||"本机研究者";}
  const budgetPolicyFields=[
    ["initial_seconds","初始处理时间（分钟）",60,21600,true],
    ["initial_calls","初始模型调用次数",3,20,false],
    ["addition_seconds","每次追加时间（分钟）",60,3600,true],
    ["addition_calls","每次追加模型调用次数",1,10,false],
    ["max_auto_additions","最多自动追加次数",0,5,false],
    ["max_total_seconds","单篇累计时间上限（分钟）",60,21600,true],
    ["max_total_calls","单篇累计模型调用上限",3,20,false]
  ];
  function parseBudgetPolicy(values){
    const result={};
    for(const [key,label,min,max,minutes] of budgetPolicyFields){
      const raw=values[key];
      if(raw==null||String(raw).trim()==="")throw new Error(`请填写${label}。`);
      const number=Number(raw),scaled=minutes?number*60:number,integer=Math.round(scaled);
      if(!Number.isFinite(number)||Math.abs(scaled-integer)>1e-6||integer<min||integer>max)
        throw new Error(`${label}应为 ${minutes?min/60:min}—${minutes?max/60:max} ${minutes?"分钟，且精确到秒":"之间的整数"}。`);
      result[key]=integer;
    }
    if(result.max_total_seconds<result.initial_seconds)throw new Error("单篇累计时间上限不能小于初始处理时间。");
    if(result.max_total_calls<result.initial_calls)throw new Error("单篇累计模型调用上限不能小于初始调用次数。");
    return result;
  }
  function budgetPolicyForm(policy,{title="每篇文献额度",legacy=false}={}){
    const inputs={},message=el("p",{class:"budget-validation",role:"alert"}),summary=el("p",{class:"budget-policy-summary","aria-live":"polite"});
    let changed=()=>{};
    const fields=budgetPolicyFields.map(([key,label,min,max,minutes])=>{
      const input=el("input",{class:"field-input",type:"number",min:minutes?min/60:min,max:minutes?max/60:max,step:minutes?"any":"1",value:policy?.[key]==null?"":policy[key]/(minutes?60:1),"data-budget-field":key,readonly:legacy&&key.startsWith("initial_")});
      inputs[key]=input;input.addEventListener("input",()=>{paint();changed();});
      return field(label,input,legacy&&key.startsWith("initial_")?"沿用原计划，不能改写历史初始额度。":key==="max_auto_additions"?"填 0 可关闭自动追加；手动追加仍受累计上限约束。":null);
    });
    const getPolicy=()=>parseBudgetPolicy(Object.fromEntries(Object.entries(inputs).map(([key,input])=>[key,input.value])));
    function paint(){
      try{
        const value=getPolicy();message.textContent="";
        summary.textContent=`每篇初始 ${durationText(value.initial_seconds)} / ${value.initial_calls} 次调用；最多自动追加 ${value.max_auto_additions} 次，每次最多 ${durationText(value.addition_seconds)} / ${value.addition_calls} 次。单篇累计不超过 ${durationText(value.max_total_seconds)} / ${value.max_total_calls} 次，先达到的限制生效。`;
      }catch(error){message.textContent=error.message;summary.textContent="请先修正额度设置，再提交任务。";}
    }
    const element=el("section",{class:"budget-policy-panel"},el("h3",{},title),
      el("p",{class:"small-text muted"},legacy?"此旧任务没有批次额度政策。为剩余工作制定政策后先检查方案；原计划和已用额度保留。":`以下额度分别应用到本次每篇文献，并非所有文献共用一份。模型为 ${currentModelLabel()}。`),
      el("div",{class:"budget-fields"},fields),message,summary,
      el("p",{class:"tiny muted"},"模型调用次数与读取原文、查看图表等工具次数分别计算。自动追加只用于可核验的未完成阶段；来源改变、没有实质进展或达到累计上限时保留结果并等待处理。"));
    paint();
    return {element,getPolicy,onChange:fn=>{changed=fn;},get valid(){try{getPolicy();return true;}catch{return false;}},setDisabled:value=>Object.values(inputs).forEach(input=>{input.disabled=value;})};
  }
  function frozenBudgetPolicyView(policy){
    if(!policy)return null;
    return el("div",{class:"budget-policy-frozen"},el("h4",{},"本篇已设定的额度政策"),
      el("p",{},`初始 ${durationText(policy.initial_seconds)} / ${policy.initial_calls} 次；每次追加最多 ${durationText(policy.addition_seconds)} / ${policy.addition_calls} 次；最多自动追加 ${policy.max_auto_additions} 次。`),
      el("p",{},`单篇累计上限：${durationText(policy.max_total_seconds)} / ${policy.max_total_calls} 次调用。`),
      el("p",{class:"tiny muted"},"已设定的政策保留原值，不能通过此入口提高累计上限。"));
  }
  function extractionModal(ids){
    if(!ids.length){toast("请先选择要处理的文献",true);return;}
    const selected=S.papers.filter(p=>ids.includes(p.id));
    const existing=selected.filter(p=>extractionInfo(p).has_candidate||extractionInfo(p).available_result||S.records.some(r=>recordPaperId(r)===p.id));
    const allDev=selected.length&&selected.every(p=>p.cohort_role==="development");
    const role=select([["development","开发文献"],["unassigned","普通待处理文献"],["heldout","保留验证文献"]],allDev?"development":"unassigned");
    const auto=el("input",{type:"checkbox"}),restart=el("input",{type:"checkbox"});
    const budget=S.status.budget_policy_defaults?budgetPolicyForm(S.status.budget_policy_defaults):null;
    let submitting=false;
    const body=el("div",{},el("div",{class:"inline-notice"},`本次只处理选中的 ${ids.length} 篇文献。模型为 ${currentModelLabel()}。`),
      el("p",{class:"small-text muted"},S.status.default_extraction_scope==="research_core"?"默认完整理解正文与 SI，抽取关键结果、条件比较、稳定性和必要设计对照。密集曲线内部点按需补抽，未补点不视为原文未报告。":"当前服务仍使用已加载的旧规则。新核心抽取方式需正常重启服务后启用；已有结果不会被删减。"),
      el("ul",{class:"small-text",style:"padding-left:20px;max-height:180px;overflow:auto"},selected.map(p=>el("li",{style:"margin-bottom:9px"},p.folder_number!=null?`文献 ${p.folder_number} · `:"",p.title||p.folder_name))),
      field("本次文献用途",role,"已用于规则修订的文献不能重新标为独立验证。"));
    body.append(budget?budget.element:el("p",{class:"inline-notice"},"当前后台尚未提供批次额度设置，请重启网页后台后再开始抽取。"));
    if(existing.length)body.append(el("div",{class:"run-concerns"},el("strong",{},`所选文献中 ${existing.length} 篇已有候选或结果`),
      el("p",{class:"small-text"},"此入口将重新阅读全文并抽取。若只需完成失败处，请到抽取任务中选择“处理未完成任务”。"),
      el("label",{class:"checkbox-label"},restart,el("span",{},"我确认对这些已有结果的文献重新全文抽取，保留旧版本。"))));
    body.append(el("label",{class:"checkbox-label"},auto,el("span",{},"自动审查完成并通过准入检查后，自动同步到新Neo4j数据库。")));
    body.append(el("p",{class:"budget-authorization"},"点击开始即采用上方每篇额度，并允许在设定次数与累计上限内自动追加。追加处理保留历史记录，不重新全文抽取，也不自动写入 Neo4j。"));
    const d=openModal("开始文献抽取","正文/SI联合理解 → 抽取 → 回查与修订 → 保存可追溯结果",body);
    d.classList.add("budget-dialog");d.addEventListener("close",()=>d.classList.remove("budget-dialog"),{once:true});
    const run=button(existing.length?"开始新一轮完整抽取":"开始处理所选文献",()=>{
      if(existing.length&&!restart.checked)return;
      if(!budget||submitting)return;
      submitting=true;budget.setDisabled(true);update();
      return perform(async()=>{await post("/api/extractions",{paper_ids:ids,auto_publish:auto.checked,cohort_role:role.value,budget_policy:budget.getPolicy()});d.close();toast("抽取任务已加入后台队列，单篇额度政策已保存");await loadBase();route("jobs");},run).finally(()=>{submitting=false;budget.setDisabled(false);update();});
    },"primary","play");
    const update=()=>{run.disabled=submitting||(existing.length>0&&!restart.checked)||!budget?.valid;};
    restart.addEventListener("change",update);if(budget)budget.onChange(update);update();
    body.append(el("div",{class:"form-actions"},button("取消",()=>d.close()),run));
  }
  const recoveryActionNames={continue_candidate:"继续处理已有结果",correct_reading:"补正阅读记录",complete_audit:"补完审查",resume_initial:"继续首次阅读与抽取",resume_extraction:"从已完成全文理解继续抽取",repair_identity:"校正文献编号并继续审查",resume_audit:"继续未完成的局部补审",extend_budget:"追加额度并继续",reextract:"重新全文抽取",retry_recheck:"继续原文回查"};
  function jobName(j){
    const names={scan:"文献目录识别",extract:"全文理解与抽取",extraction:"全文理解与抽取",publish:"图谱版本同步",recheck:"原文定向回查",ask:"文献证据问答",qa:"文献证据问答"};
    if(j.kind==="recover")return recoveryActionNames[j.payload?.recovery_action||j.payload?.action]||"已有任务续处理";
    return j.payload?.digitization_targets?.length?`按需曲线补抽 · ${j.payload.digitization_targets.length} 个系列`:names[j.kind]||"后台任务";
  }
  function jobPaper(j){return S.papers.find(p=>p.id===(j.paper_id||j.payload?.paper_id))||{};}
  function jobTitle(j){return j.paper_title||j.payload?.title||jobPaper(j).title||jobPaper(j).folder_name||jobName(j);}
  function jobNumber(j){return j.job_number==null?"任务号待登记":`任务 #${j.job_number}`;}
  function jobIdentity(j){
    const parts=[];
    if(j.paper_id||j.payload?.paper_id||j.folder_number!=null)parts.push(numberTag(j));
    parts.push(el("span",{class:"task-number"},jobNumber(j)));
    parts.push(el("span",{class:"job-kind"},jobName(j)));
    return el("div",{class:"job-identity"},parts);
  }
  function jobDisplayBadge(j){return stateBadge(j.recovered?"completed":j.display_status||j.status,j.display_status_label);}
  function jobResolved(j){return j.recovered===true;}
  function jobCanRecover(j){return !jobResolved(j)&&["failed","interrupted","cancelled","incomplete"].includes(j.status)&&["extract","extraction","recheck","recover"].includes(j.kind);}
  function recoveryBudgetView(budget,title=null){
    if(!budget)return null;
    if(typeof budget==="string")return el("p",{class:"recovery-budget-note"},budget);
    const remainingSeconds=budget.remaining_seconds??budget.remaining_time_seconds;
    const remainingCalls=budget.remaining_calls??budget.remaining_model_calls;
    const items=[];
    if(budget.policy==="lineage_remaining"){
      if(typeof budget.original_seconds==="number")items.push(["原处理链时长上限",durationText(Math.max(0,budget.original_seconds))]);
      if(typeof budget.original_calls==="number")items.push(["原处理链调用上限",Math.max(0,budget.original_calls)+" 次"]);
    }
    const usedSeconds=budget.used_seconds??budget.used_model_seconds;
    if(typeof usedSeconds==="number")items.push(["已消耗模型时长",durationText(Math.max(0,usedSeconds))]);
    if(typeof budget.used_calls==="number")items.push(["已使用模型调用",Math.max(0,budget.used_calls)+" 次"]);
    if(typeof remainingSeconds==="number")items.push(["可用模型时长",durationText(Math.max(0,remainingSeconds))]);
    else if(typeof budget.seconds==="number")items.push(["本次时长上限",durationText(Math.max(0,budget.seconds))]);
    if(typeof remainingCalls==="number")items.push(["可用模型调用",Math.max(0,remainingCalls)+" 次"]);
    else if(typeof budget.calls==="number")items.push(["本次调用上限",Math.max(0,budget.calls)+" 次"]);
    if(typeof budget.remaining_revisions==="number")items.push(["可用修订次数",Math.max(0,budget.remaining_revisions)+" 次"]);
    if(!items.length&&!budget.message&&!budget.description&&!budget.exhausted)return null;
    return el("div",{class:"recovery-budget"+(budget.exhausted?" exhausted":"")},
      title?el("h4",{},title):null,
      items.length?el("dl",{},items.map(([label,value])=>el("div",{},el("dt",{},label),el("dd",{},value)))):null,
      (budget.message||budget.description)?el("p",{},budget.message||budget.description):null,
      budget.exhausted?el("p",{},"该恢复入口的可用额度已耗尽；不会自动改为重新全文抽取。"):null,
      el("p",{class:"tiny"},"额度沿用该恢复流程的约束，不代表完成所需时间；执行前会重新核验。"));
  }
  function jobRecoveryNote(j){
    if(!jobResolved(j))return null;
    return el("div",{class:"job-recovery-note"},el("strong",{},j.display_status_label||"后续已修复完成"),
      el("p",{},j.resolution_summary||"该文献已有后续完成结果，原任务的失败记录继续保留。"),
      el("span",{class:"tiny"},`本次历史运行：${statusNames[j.status]||j.status}`));
  }
  function jobBudgetDecisionNote(j){
    const decision=j.payload?.auto_budget_decision;
    if(!decision||decision.action==="not_applicable")return null;
    const queued=!!decision.queued_job_id;
    const box=el("div",{class:"job-budget-note"+(queued?"":" waiting")},
      el("strong",{},decision.action==="resumed"?"已用原额度安排续处理":queued?"已安排自动追加并继续":"自动续处理说明"),
      decision.reason?el("p",{},decision.reason):null,
      el("span",{class:"tiny"},queued?"原任务的结果、失败原因和已用额度保留；后续任务仍须完成审查。":"已有成果保留，未完成状态不会自动变为审查通过。"));
    if(queued)box.append(button("查看后续任务",()=>jobModal(decision.queued_job_id),"small text-button","arrow"));
    return box;
  }
  function extensionBudgetView(plan){
    const budget=plan.budget_summary||{},items=[];
    for(const [key,label,time] of [["used_seconds","累计已用模型时间",true],["used_calls","累计已用调用",false],
      ["allocated_seconds","已经分配的时间额度",true],["allocated_calls","已经分配的调用额度",false],
      ["max_total_seconds","单篇累计时间上限",true],["max_total_calls","单篇累计调用上限",false],
      ["auto_additions","已经自动追加",false]]){
      if(typeof budget[key]==="number")items.push([label,time?durationText(budget[key]):budget[key]+" 次"]);
    }
    const addition=plan.proposed_addition||{};
    return el("section",{class:"budget-extension-summary"},el("h3",{},"累计用量与本次追加"),
      items.length?el("dl",{class:"budget-ledger-grid"},items.map(([label,value])=>el("div",{},el("dt",{},label),el("dd",{},value)))):el("p",{class:"small-text muted"},"尚未取得可核验的累计用量，请查看下方原因。"),
      typeof addition.seconds==="number"&&typeof addition.calls==="number"?el("div",{class:"budget-proposed"},el("span",{},"本次拟追加"),
        el("strong",{},`${durationText(addition.seconds)} / ${addition.calls} 次模型调用`)):null,
      el("p",{class:"tiny muted"},"以上按本篇处理链累计。追加另行记录，原计划、历史消耗和候选版本保持可追溯。"));
  }
  async function budgetExtensionModal(job){
    const body=el("div",{class:"budget-extension-workspace"},loading("正在检查本篇累计额度与已有成果…"));
    const dialog=openModal("追加额度并继续","只为尚未完成的阶段追加有上限的额度，保留已有成果。",body);
    dialog.classList.add("budget-dialog");dialog.addEventListener("close",()=>dialog.classList.remove("budget-dialog"),{once:true});
    let requestSerial=0;
    async function load(policyDraft=null){
      const serial=++requestSerial;
      body.replaceChildren(loading("正在核对处理链、剩余工作与可追加额度…"),
        el("p",{class:"recovery-checking-note",role:"status"},"此处只检查本地记录，不启动模型，也不会重新全文抽取。"));
      try{
        const plan=policyDraft?await post("/api/jobs/"+enc(job.id)+"/budget-extension/preview",{budget_policy:policyDraft})
          :await api("/api/jobs/"+enc(job.id)+"/budget-extension");
        if(!body.isConnected||!dialog.open||serial!==requestSerial)return;
        const editor=plan.policy_editable===true?budgetPolicyForm(plan.policy||policyDraft||S.status.budget_policy_defaults,{title:"为本篇剩余工作设定额度政策",legacy:true}):null;
        let busy=false,stale=false;
        const failure=el("div",{class:"recovery-submit-error",role:"alert"});
        const changed=el("p",{class:"budget-preview-stale",role:"status"});
        const reload=button(editor?"检查追加方案":"重新检查追加方案",()=>{
          try{return load(editor?editor.getPolicy():null);}catch(error){failure.textContent=error.message;}
        },"small text-button","clock");
        const start=button("追加额度并继续",submit,"primary","play");
        function update(){
          start.disabled=busy||stale||plan.available!==true||!plan.base_fingerprint||(editor&&!editor.valid);
          reload.disabled=busy||(editor&&!editor.valid);
          if(editor)editor.setDisabled(busy);
          changed.textContent=stale?"额度设置或运行状态已变化，请重新检查追加方案后再提交。":"";
          start.replaceChildren(icon("play"),busy?"正在提交追加任务…":"追加额度并继续");
        }
        if(editor)editor.onChange(()=>{stale=true;update();});
        async function submit(){
          if(busy||stale||plan.available!==true||!plan.base_fingerprint||(editor&&!editor.valid))return;
          busy=true;failure.replaceChildren();update();
          try{
            const request={base_fingerprint:plan.base_fingerprint};
            if(editor)request.budget_policy=editor.getPolicy();
            await post("/api/jobs/"+enc(job.id)+"/budget-extension",request);
            dialog.close();toast("追加额度续处理已加入队列；历史结果保留，不自动入库");
            await loadBase();route("jobs",null,{});
          }catch(error){
            stale=true;failure.replaceChildren(el("strong",{},"追加任务未提交成功"),el("p",{},error.message),
              el("p",{},"请重新检查，页面不会改为重新全文抽取或绕过累计上限。"));
          }finally{busy=false;if(dialog.open&&serial===requestSerial)update();}
        }
        const continuation=plan.continuation||{};
        body.replaceChildren(jobIdentity(job),el("h3",{class:"recovery-paper-title"},jobTitle(job)),
          el("section",{class:"budget-continuation"},el("h3",{},"追加后继续完成什么"),
            el("strong",{},continuation.label||"等待确认可继续的阶段"),
            continuation.description?el("p",{},continuation.description):null,
            plan.parent_run_id?el("div",{class:"recovery-reference"},el("span",{},"依据运行"),el("code",{},plan.parent_run_id)):null),
          extensionBudgetView(plan),editor?editor.element:frozenBudgetPolicyView(plan.policy));
        if(plan.reason)body.append(el("div",{class:plan.available===true?"note-panel":"run-concerns"},
          el("strong",{},plan.available===true?"本次方案说明":"当前不能追加"),el("p",{class:"small-text"},plan.reason)));
        body.append(changed,failure,el("p",{class:"budget-authorization"},editor?
          "点击追加即采用上方本篇政策，并在其自动次数与累计上限内允许后续自动补充。所有处理沿用已有成果，发现科学疑点仍须修订与审查。":
          "本次追加受已设定的累计上限约束。已有成果继续使用，尚未完成的科学审查仍须完成。"),
          el("p",{class:"recovery-boundary"},`模型：${currentModelLabel()} · 历史用量不重置 · 不重新全文抽取 · 本次操作不写入 Neo4j`),
          el("div",{class:"form-actions recovery-footer"},reload,button("取消",()=>dialog.close()),start));
        update();
      }catch(error){
        if(!body.isConnected||!dialog.open||serial!==requestSerial)return;
        body.replaceChildren(jobIdentity(job),errorPanel(error.status===404?"当前后台尚未提供追加额度接口，请重启网页后台后重新检查。":error,()=>load(policyDraft)),
          el("p",{class:"small-text muted"},"没有启动模型，也没有修改历史额度。"));
      }
    }
    await load();
  }
  function jobActions(j,{details=true}={}){
    const actions=[];
    if(details)actions.push(button("运行详情",()=>jobModal(j.id),"small","layers"));
    const resultId=j.current_result_id||j.recovery?.record_id||j.result_record_id||j.result?.record_id;
    if(resultId&&S.records.some(r=>r.id===resultId))actions.push(button(j.recovered?"查看修复后结果":"查看结果",()=>route("records",resultId),"small text-button"));
    const originalId=j.result_record_id||j.result?.record_id;
    if(originalId&&originalId!==resultId&&S.records.some(r=>r.id===originalId))actions.push(button("查看本次历史结果",()=>route("records",originalId),"small text-button"));
    if(jobCanRecover(j))actions.push(button("处理未完成任务",()=>recoveryModal(j),"small text-button","play"));
    else if(!jobResolved(j)&&["failed","interrupted","cancelled","incomplete"].includes(j.status))actions.push(button("重新执行此任务",()=>perform(async()=>{await post("/api/jobs/"+enc(j.id)+"/retry");await loadBase();jobs();toast("已创建新的任务，原任务记录保留");}),"small text-button"));
    if(["pending","queued"].includes(j.status))actions.push(button("取消排队",()=>perform(async()=>{await post("/api/jobs/"+enc(j.id)+"/cancel");await loadBase();jobs();}),"small text-button"));
    if(jobCanRecover(j)&&["extract","extraction","recover"].includes(j.kind))actions.push(button("追加额度并继续",()=>budgetExtensionModal(j),"small text-button","plus"));
    return actions;
  }
  async function recoveryModal(job){
    const body=el("div",{class:"recovery-workspace"},loading("正在检查已保存的候选与恢复入口…"));
    const dialog=openModal("处理未完成任务","先查看已有结果与可用方式，再明确启动；查看此页面不会调用模型。",body);
    dialog.classList.add("recovery-dialog");dialog.addEventListener("close",()=>dialog.classList.remove("recovery-dialog"),{once:true});
    let requestSerial=0;
    async function load(){
      const serial=++requestSerial;
      body.replaceChildren(loading("正在检查已有产物与恢复证明…"),el("p",{class:"recovery-checking-note",role:"status"},"核验来源和运行记录可能需要几十秒。此处仅做本地检查，不调用模型，也不会重新抽取。"));
      try{
        const plan=await api("/api/jobs/"+enc(job.id)+"/recovery");
        if(!body.isConnected||!dialog.open||serial!==requestSerial)return;
        const choices=Array.isArray(plan.actions)?plan.actions:[];
        const recommended=choices.find(a=>a.id===plan.recommended_action&&a.available===true&&a.id!=="reextract");
        const budgetOption=choices.find(a=>a.budget_preview===true);
        const selectable=a=>a&&(a.available===true||a.budget_preview===true);
        let selected=recommended?.id||null,busy=false,stale=false;
        if(!selected&&budgetOption&&budgetOption.id!=="reextract")selected=budgetOption.id;
        const summary=plan.candidate_summary||{};
        const saved=el("section",{class:"recovery-saved"},el("h3",{},"本次依据的已有结果"));
        if(typeof summary==="string")saved.append(el("p",{},summary));
        else{
          if(summary.summary)saved.append(el("p",{},summary.summary));
          const count=summary.observation_count??summary.observations;
          saved.append(el("p",{},typeof count==="number"?`已保存 ${count} 条候选观测。`:summary.has_candidate===false?"尚未找到可继续使用的候选 JSON。":"候选及审查记录的可复用范围见下方处理方式。"));
          if(summary.has_candidate!==false&&summary.status)saved.append(stateBadge("candidate_unverified","候选已保存"));
          const candidate=summary.candidate_file||summary.candidate_id||summary.path;
          if(candidate)saved.append(el("div",{class:"recovery-reference"},el("span",{},"候选版本"),el("code",{},candidate)));
        }
        if(plan.parent_run_id)saved.append(el("div",{class:"recovery-reference"},el("span",{},"本次依据的运行"),el("code",{},plan.parent_run_id)));
        if(plan.original_run_id&&plan.parent_run_id&&plan.original_run_id!==plan.parent_run_id)saved.append(
          el("p",{class:"small-text"},"已找到后续保存版本，将以此版本继续。"),
          el("details",{class:"details-section"},el("summary",{},"查看原任务运行"),el("code",{class:"recovery-original-run"},plan.original_run_id)));
        const overallBudget=recoveryBudgetView(plan.budget_summary,plan.budget_summary?.policy==="lineage_remaining"?"原处理链剩余额度":"原运行剩余额度");if(overallBudget)saved.append(overallBudget);
        saved.append(el("p",{class:"tiny muted"},"发现候选或可用入口不等于审查通过。执行前仍会检查来源、版本及剩余预算；检查不通过时会停止并保留记录。"));
        if(["extract","extraction","recover"].includes(job.kind))saved.append(button("检查追加额度并继续",()=>{dialog.close();budgetExtensionModal(job);},"small text-button","plus"));
        if(plan.current_result_id)saved.append(el("div",{class:"recovery-existing-result"},button("打开已有结果",()=>{dialog.close();route("records",plan.current_result_id);},"small text-button","arrow"),
          el("p",{},"具体数值、条件或遗漏问题，可在结果页进入人工抽查后提交定向回查。")));
        const optionList=el("fieldset",{class:"recovery-options"},el("legend",{},"选择处理方式"));
        const optionNodes=new Map();
        const confirm=el("input",{type:"checkbox",id:"recovery-reextract-confirm"});
        const confirmation=el("label",{class:"checkbox-label recovery-reextract-confirm",for:confirm.id},confirm,el("span",{},"我确认重新阅读全文并抽取，可能重复已有工作；原结果和失败记录仍保留。"));
        const selectedNote=el("div",{class:"recovery-selected-note",role:"status"});
        const failure=el("div",{class:"recovery-submit-error",role:"alert"});
        const reload=button("重新检查可用方式",load,"small text-button","clock");
        const start=button("选择处理方式后开始",submit,"primary","play");
        for(const action of choices){
          const available=selectable(action),radio=el("input",{type:"radio",name:"recovery-action",value:action.id,checked:selected===action.id,disabled:!available});
          const title=el("span",{class:"recovery-option-title"},el("strong",{},action.label||recoveryActionNames[action.id]||action.id),
            action.id===recommended?.id?el("span",{class:"badge green"},"建议优先"):null,action.budget_preview?el("span",{class:"badge"},"需追加额度"):!available?el("span",{class:"badge"},action.applicable===false?"不适用":action.unavailable_label||"暂不可用"):null);
          const option=el("label",{class:"recovery-option"+(!available?" unavailable":"")},radio,el("span",{class:"recovery-option-body"},title,
            action.description?el("span",{class:"recovery-option-description"},action.description):null,
            action.reason?el("span",{class:"recovery-option-reason"},action.reason):null,available?recoveryBudgetView(action.budget_summary||action.budget):null));
          optionNodes.set(action.id,{option,radio});optionList.append(option);
          radio.addEventListener("change",()=>{selected=action.id;confirm.checked=false;update();});
        }
        function update(){
          const action=choices.find(a=>a.id===selected&&selectable(a)),restart=action?.id==="reextract";
          for(const [id,item] of optionNodes){item.option.classList.toggle("selected",id===selected);item.radio.disabled=busy||stale||!selectable(choices.find(a=>a.id===id));}
          confirmation.hidden=!restart;confirm.disabled=busy||stale;
          start.disabled=busy||stale||!action||(restart&&!confirm.checked)||!plan.base_fingerprint;
          start.replaceChildren(icon("play"),busy?"正在提交任务…":action?.budget_preview?"查看本次修订所需额度":action?"开始"+(action.label||recoveryActionNames[action.id]||"处理"):"选择处理方式后开始");
          const nextNotes={resume_initial:"将复用已导入来源，开始此前尚未执行的首次阅读与抽取；没有旧候选可以直接续审。",resume_extraction:"将复用已核验的全文理解档案，从尚未完成的抽取阶段继续，并保留必要原文回查；沿原处理链扣除已消耗额度，不重复执行首次全文理解。",repair_identity:"只校正可核验的系统文献编号，保留科学数据，再继续尚未完成的审查。",resume_audit:"将保留候选和既有审查，沿原运行链扣除已消耗的额度后继续局部补审；预算不重置，也不重新审查整稿。"};
          selectedNote.textContent=action?(restart?"将新建完整抽取任务，不使用续审替代。":nextNotes[action.id]||"将新建后续任务，依据已有结果处理；不覆盖原运行。"):(choices.some(a=>a.available===true&&a.id!=="reextract")?"请选择符合当前阶段的处理方式。":"当前尚无可验证的续处理入口。重新全文抽取是独立选项，并非系统要求；请先查看上方原因。");
          reload.disabled=busy;
          if(action?.budget_preview)selectedNote.textContent=action.reason;
        }
        async function submit(){
          const action=choices.find(a=>a.id===selected&&selectable(a));
          if(busy||stale||!action||!plan.base_fingerprint||(action.id==="reextract"&&!confirm.checked))return;
          if(action.budget_preview){dialog.close();budgetExtensionModal(job);return;}
          busy=true;failure.replaceChildren();update();
          try{
            await post("/api/jobs/"+enc(job.id)+"/recover",{action:action.id,base_fingerprint:plan.base_fingerprint});
            dialog.close();toast("已加入"+(action.label||recoveryActionNames[action.id]||"后续处理")+"任务；原结果保留，不自动入库");
            await loadBase();route("jobs",null,{});
          }catch(error){
            stale=true;failure.replaceChildren(el("strong",{},"任务未提交成功"),el("p",{},error.status===404?"后台尚未提供恢复接口，请重启网页后台后重新检查。":error.message),
              el("p",{},"请重新检查可用方式后再提交；页面不会自动改为全文抽取。"));
          }finally{busy=false;if(dialog.open&&serial===requestSerial)update();}
        }
        confirm.addEventListener("change",update);
        body.replaceChildren(jobIdentity(job),el("h3",{class:"recovery-paper-title"},jobTitle(job)),saved);
        if(typeof plan.recovery_explanation==="string"&&plan.recovery_explanation)body.append(el("p",{class:"recovery-checking-note",role:"status"},plan.recovery_explanation));
        body.append(el("p",{class:"small-text muted"},"处理方式按已完成的阶段开放，不需要启用所有选项；请优先使用适用的续处理入口。"));
        if(plan.blockers?.length)body.append(el("div",{class:"run-concerns"},el("strong",{},"需要先处理的事项"),el("ul",{},plan.blockers.map(x=>el("li",{},typeof x==="string"?x:x.message||x.reason||"当前恢复条件不满足")))));
        body.append(optionList,confirmation,selectedNote,failure,el("p",{class:"recovery-boundary"},`模型：${currentModelLabel()} · 历史版本保留 · 本次操作不写入 Neo4j`),
          el("div",{class:"form-actions recovery-footer"},reload,button("取消",()=>dialog.close()),start));
        const diagnostics=choices.filter(action=>action.technical_reason);
        if(diagnostics.length){
          const diagnosticDetails=el("details",{class:"details-section recovery-technical"},el("summary",{},"技术诊断（排查时查看）"),
            diagnostics.map(action=>el("div",{class:"recovery-technical-item"},el("strong",{},action.label||recoveryActionNames[action.id]||action.id),
              el("pre",{},action.technical_reason))));
          body.insertBefore(diagnosticDetails,body.querySelector(".recovery-boundary"));
        }
        update();
      }catch(error){
        if(!body.isConnected||!dialog.open||serial!==requestSerial)return;
        body.replaceChildren(jobIdentity(job),errorPanel(error.status===404?"后台尚未提供恢复接口，请重启网页后台后重新检查。":error,load),
          el("p",{class:"small-text muted"},"尚未启动模型任务，也没有重新抽取。"));
      }
    }
    await load();
  }
  function jobs(){
    const m=$("#main");
    const queueToggle=button(S.status.paused?"继续任务队列":"暂停后续任务",()=>perform(async()=>{await post("/api/queue",{paused:!S.status.paused});await loadBase();jobs();}),S.status.paused?"primary":"");
    queueToggle.id="queue-toggle";
    m.replaceChildren(pageHeader("抽取任务","任务号用于区分每次运行；文献序号对应原始文件夹。同一文献的续审与重试保留独立记录。",[
      queueToggle,
      button("选择文献",()=>route("library"),"","book")],"EXTRACTION PIPELINE"));
    updateQueueControl();
    if(S.status.paused)m.append(el("div",{class:"inline-notice"},"后续队列已暂停。当前正在运行的任务不因暂停队列而丢失。"));
    const list=el("div",{class:"job-list"}),count=el("div",{class:"filter-summary"});
    const search=el("input",{class:"search-input",type:"search",placeholder:"搜索文献序号、任务号、标题或 DOI",value:S.jobQuery,"aria-label":"搜索任务",oninput:e=>{S.jobQuery=e.target.value;syncListLocation("jobs");paint();}});
    const filter=select([["all","全部任务状态"],["active","排队 / 处理中"],["completed","已完成 / 后续已修复"],["recovered","后续已修复完成"],["unfinished","当前仍未完成"],["history_failed","历史失败记录"],["cancelled","已取消"]],S.jobFilter,{"aria-label":"任务状态筛选",onchange:e=>{S.jobFilter=e.target.value;syncListLocation("jobs");paint();}});
    m.append(el("section",{class:"card job-controls"},el("div",{class:"toolbar"},el("div",{class:"search-control"},icon("search"),search),filter),count),list);
    function paint(){
      const q=S.jobQuery.trim().toLocaleLowerCase();
      const rows=S.jobs.filter(j=>!q||[jobNumber(j),j.job_number,literatureNumber(j),jobTitle(j),j.paper_doi,jobPaper(j).doi,j.folder_name].join(" ").toLocaleLowerCase().includes(q))
        .filter(j=>S.jobFilter==="all"||(S.jobFilter==="active"?["running","queued","pending"].includes(j.status)
          :S.jobFilter==="completed"?["completed","succeeded"].includes(j.status)||jobResolved(j)
          :S.jobFilter==="recovered"?jobResolved(j)
          :S.jobFilter==="cancelled"?j.status==="cancelled"
          :["failed","incomplete","interrupted"].includes(j.status)&&(S.jobFilter==="history_failed"||!jobResolved(j))));
      count.textContent=`显示 ${rows.length} / ${S.jobs.length} 个任务 · 文献序号沿用原文件夹编号`;
      if(!rows.length){list.replaceChildren(empty("没有符合条件的任务",S.jobs.length?"调整搜索词或筛选条件查看其他运行。":"选择文献开始处理后，这里会显示每个任务的阶段与结果。"));return;}
      list.replaceChildren(...rows.map(j=>el("article",{class:"job-card"},jobIdentity(j),
        el("div",{class:"job-top"},el("div",{class:"job-heading"},el("h2",{class:"job-title"},jobTitle(j)),
          el("div",{class:"job-meta"},`${shortDate(j.created_at)} 创建 · ${shortDate(j.updated_at)} 更新`),
          (j.paper_doi||jobPaper(j).doi)?el("div",{class:"job-meta"},j.paper_doi||jobPaper(j).doi):null),jobDisplayBadge(j)),
        jobRecoveryNote(j),jobBudgetDecisionNote(j),el("div",{class:"job-message"},j.summary||j.stage||"等待执行"),
        el("div",{class:"job-footer"},el("span",{class:"small-text muted"},"当前阶段："+(j.stage||"尚未记录")),el("div",{class:"table-actions"},jobActions(j))))));
    }
    paint();
  }
  function toolWorkText(counts,emptyLabel="工具未按此阶段单独登记"){
    const labels={wt_read_blocks:"原文片段读取",wt_page_text:"页面文字读取",wt_view:"图表查看",wt_query_candidate:"候选字段核对",wt_patch_record:"局部修订",wt_submit_record:"保存候选",wt_revision_state:"修订状态检查",wt_record_schema:"字段规范查询"};
    return Object.entries(counts||{}).filter(([,n])=>typeof n==="number"&&n>0).map(([name,n])=>`${labels[name]||"其他工具操作"} ${n} 次`).join(" · ")||emptyLabel;
  }
  function stageBadge(status){return stateBadge(status,({pending:"未执行",not_started:"未执行",not_run:"未执行",completed:"已完成",failed:"未完成",running:"正在进行",incomplete:"尚未完成",interrupted:"已中断",skipped:"本次无需执行",retained:"已保留",saved:"已保存"})[status]||statusNames[status]||"状态待确认");}
  async function jobModal(id){
    const body=el("div",{class:"run-detail"},loading("正在读取已保存的运行记录…"));
    const d=openModal("运行详情","按实际记录展示任务做了什么，以及哪些工作仍未完成。",body);
    d.classList.add("job-detail-dialog");d.addEventListener("close",()=>d.classList.remove("job-detail-dialog"),{once:true});
    let requestSerial=0;
    async function load(){
      const serial=++requestSerial;
      try{
        const detail=await api("/api/jobs/"+enc(id));if(!body.isConnected||!d.open||serial!==requestSerial)return;
        const j=detail.job||S.jobs.find(x=>x.id===id)||{},outcome=detail.outcome||{},timings=detail.timings||{};
        const top=el("div",{class:"run-detail-top"},jobIdentity(j),el("h3",{},jobTitle(j)),el("div",{class:"badges"},badge(j.status),outcome.label?stateBadge(outcome.state,outcome.label):null),el("p",{},detail.summary||j.summary||j.stage||"没有登记运行摘要"));
        const resultAction=outcome.record_id||j.result_record_id;
        const actions=el("div",{class:"table-actions"},button("刷新详情",()=>perform(load),"small","clock"));
        if(resultAction&&S.records.some(r=>r.id===resultAction))actions.append(button("打开本次结果",()=>{d.close();route("records",resultAction);},"small","arrow"));
        if(j.recovered&&(j.current_result_id||j.recovery?.record_id))actions.append(button("查看修复后结果",()=>{d.close();route("records",j.current_result_id||j.recovery.record_id);},"small primary","check"));
        if(jobCanRecover(j))actions.append(button("处理未完成任务",()=>{d.close();recoveryModal(j);},"small primary","play"));
        if(jobCanRecover(j)&&["extract","extraction","recover"].includes(j.kind))actions.append(button("追加额度并继续",()=>{d.close();budgetExtensionModal(j);},"small","plus"));
        top.append(actions);
        body.replaceChildren(top,...[jobRecoveryNote(j),jobBudgetDecisionNote(j),frozenBudgetPolicyView(j.payload?.budget_policy)].filter(Boolean),el("div",{class:"run-timing-grid"},[["排队等待",timings.queued_seconds],["执行耗时",timings.execution_seconds],["模型调用耗时",timings.model_seconds]].map(([label,value])=>el("div",{},el("span",{},label),el("strong",{},durationText(value))))));
        body.append(el("p",{class:"run-measurement-note"},"耗时来自已保存的执行记录；未登记的阶段不补算。工具调用次数不等于阅读完整性或科学准确率。"));
        if(detail.retry_of_job_number!=null)body.append(el("p",{class:"small-text muted"},`这是任务 #${detail.retry_of_job_number} 的后续处理，原记录保留。`));
        const result=el("section",{class:"run-section"},el("h3",{},"已保存的结果"),el("p",{},outcome.summary||"此任务暂无结果摘要。"));
        if(outcome.has_candidate)result.append(el("div",{class:"run-candidate-note"},outcome.observation_count!=null?`${outcome.observation_count} 条候选观测已保留` : "候选 JSON 已保留",el("span",{},"是否完成审查需结合下方阶段与待处理事项判断。")));
        if(detail.artifacts?.length)result.append(el("div",{class:"run-artifacts"},detail.artifacts.map(a=>el("div",{},el("span",{},a.label),stateBadge(a.available?"completed":"not_started",a.available?"已保存":"未生成 / 未登记")))));
        body.append(result);
        if(detail.concerns?.length)body.append(el("section",{class:"run-section run-concerns"},el("h3",{},j.recovered?"本次历史运行当时未完成的事项":"尚未完成与需要关注"),el("ul",{},detail.concerns.map(t=>el("li",{},t)))));
        if(detail.tool_counts)body.append(el("section",{class:"run-section"},el("h3",{},"本次运行累计工具操作"),el("p",{},toolWorkText(detail.tool_counts,"尚无已保存的工具调用记录")),el("p",{class:"run-measurement-note"},detail.tool_attribution_note||"旧记录未提供阶段归属时，工具操作只计入本次总量，不分摊到具体模型调用。")));
        const stages=el("section",{class:"run-section"},el("h3",{},"处理阶段"));
        if(detail.stages?.length)stages.append(el("ol",{class:"run-stages"},detail.stages.map(stage=>el("li",{class:"run-stage "+(stage.status||"")},el("div",{class:"run-stage-heading"},el("strong",{},stage.label||"处理阶段"),stageBadge(stage.status),typeof stage.elapsed_seconds==="number"?el("span",{class:"run-stage-time"},durationText(stage.elapsed_seconds)):null),el("p",{},stage.summary||"此阶段没有更详细的记录。")))));
        else stages.append(el("p",{class:"small-text muted"},"旧任务没有逐阶段记录，当前只显示其已保存状态。"));
        body.append(stages);
        if(detail.calls?.length){
          const rows=detail.calls.map(call=>el("tr",{},el("td",{},call.number??"—"),el("td",{},call.label||"模型处理",el("div",{class:"table-subtitle"},toolWorkText(call.tool_counts))),el("td",{},stateBadge(call.status,call.status_label)),el("td",{},durationText(call.elapsed_seconds))));
          body.append(el("section",{class:"run-section"},el("h3",{},"每次模型调用"),el("div",{class:"table-wrap"},el("table",{class:"data-table run-call-table"},el("thead",{},el("tr",{},["次序","本次工作 / 工具操作","结果","耗时"].map(x=>el("th",{},x)))),el("tbody",{},rows)))));
        }
        if(detail.timeline?.length)body.append(el("details",{class:"details-section run-timeline-section"},el("summary",{},`查看时间记录（${detail.timeline.length} 项）`),el("ol",{class:"run-timeline"},detail.timeline.map(event=>el("li",{},el("time",{},shortDate(event.at)),el("div",{},el("strong",{},event.label||"运行记录"),event.detail?el("p",{},event.detail):null))))));
        body.append(el("details",{class:"details-section run-debug"},el("summary",{},"技术标识（排查问题时使用）"),el("dl",{class:"info-list"},el("dt",{},"任务 ID"),el("dd",{class:"mono"},j.id||id),el("dt",{},"运行 ID"),el("dd",{class:"mono"},outcome.run_id||j.payload?.run_id||"尚未创建"))));
      }catch(error){if(body.isConnected&&serial===requestSerial)body.replaceChildren(errorPanel(error,load));}
    }
    await load();
  }
  function records(){
    const m=$("#main"),current=latestRecords();
    m.replaceChildren(pageHeader("数据与入库","当前结果包含已完成和仍待处理的候选；历史版本分别保留。",[button("刷新已有结果",()=>refresh(true),"","layers"),button("批量同步入库",()=>publishModal(),"primary","database")],"STRUCTURED EVIDENCE"));
    m.append(el("div",{class:"result-count-summary"},
      button(`已有结果 ${metricCount("records")} 篇`,()=>route("records",null,{}),"small text-button"),
      el("span",{},"="),button(`自动处理完成 ${metricCount("completed")} 篇`,()=>route("records",null,{filter:"completed"}),"small text-button"),
      el("span",{},"+"),button(`待处理候选 ${Math.max(0,metricCount("records")-metricCount("completed"))} 篇`,()=>route("records",null,{filter:"unfinished"}),"small text-button")));
    const q=el("input",{class:"search-input",type:"search",value:S.recordQuery,placeholder:"搜索文献序号、论文、DOI","aria-label":"搜索结果"});
    const body=el("div"),summary=el("div",{class:"filter-summary"});
    const filter=select([["all","全部当前结果"],["completed","自动处理完成"],["unfinished","待处理候选"],["published","最新版本已入库"],["reviewed","已人工抽查"]],S.recordFilter,{"aria-label":"结果状态筛选"});
    const historyToggle=el("input",{type:"checkbox",checked:S.showHistory,disabled:S.recordFilter!=="all"});
    const paint=()=>{
      const text=S.recordQuery.trim().toLowerCase(),historyVisible=S.showHistory&&S.recordFilter==="all";
      historyToggle.disabled=S.recordFilter!=="all";historyToggle.checked=historyVisible;
      const rows=(historyVisible?S.records:current).filter(r=>recordMatchesFilter(r,S.recordFilter))
        .filter(r=>[r.title,r.doi,recordPaperId(r),literatureNumber(r),r.folder_name].join(" ").toLowerCase().includes(text));
      const labels={all:"全部当前结果",completed:"自动处理完成",unfinished:"待处理候选",published:"最新版本已入库",reviewed:"已人工抽查"};
      summary.replaceChildren(el("span",{},historyVisible?`显示 ${rows.length} 个结果版本（同一篇可有多个版本）`:`${labels[S.recordFilter]||labels.all} · 显示 ${rows.length} 篇`),
        el("span",{},S.recordFilter==="reviewed"?"仅代表已记录的实际核查范围":"按当前版本统计，与总览使用相同口径"));
      body.replaceChildren(recordsTable(rows));
    };
    q.addEventListener("input",()=>{S.recordQuery=q.value;syncListLocation("records");paint();});
    filter.addEventListener("change",()=>{S.recordFilter=filter.value;if(S.recordFilter!=="all")S.showHistory=false;syncListLocation("records");paint();});
    historyToggle.addEventListener("change",()=>{S.showHistory=historyToggle.checked;syncListLocation("records");paint();});
    m.append(el("section",{class:"card"},el("div",{class:"toolbar"},el("div",{class:"search-control"},icon("search"),q),filter,el("div",{class:"toolbar-right"},el("label",{class:"checkbox-label"},historyToggle,"显示历史版本"))),summary,body));paint();
  }
  const publicationRequests = new Set();
  function publicationOptions(recordId) {
    const active = new Set(publicationRequests);
    for (const job of S.jobs) {
      if (job.kind === "publish" && ["queued", "pending", "running"].includes(job.status)) active.add(job.payload?.record_id);
    }
    const eligible = [];
    let published = 0, pending = 0, blocked = 0;
    for (const row of latestRecords()) {
      if (recordId != null && row.id !== recordId) continue;
      if (row.source_change) blocked++;
      else if (row.published_current ?? ["published", "loaded_as_candidate"].includes(row.publish_status)) published++;
      else if (active.has(row.id)) pending++;
      else if (row.admissible === true) eligible.push(row);
      else blocked++;
    }
    return {eligible, published, pending, blocked};
  }

  function publishModal(recordId = null) {
    const {eligible, published, pending, blocked} = publicationOptions(recordId);
    const counts = [`可同步 ${eligible.length} 篇`, `最新版本已入库 ${published} 篇`];
    if (pending) counts.push(`排队或同步中 ${pending} 篇`);
    if (blocked) counts.push(`尚未通过入库检查 ${blocked} 篇`);
    const body = el("div", {}, el("p", {class:"small-text"}, counts.join(" · ")));
    const dialog = openModal(recordId === null ? "批量同步入库" : "同步到图谱", "同步通过入库检查的文献当前版本到 Neo4j。", body);
    const close = button("关闭", () => dialog.close());
    if (!eligible.length) {
      const message = recordId !== null && !published && !pending && !blocked
        ? "此版本已不在当前文献结果中。请在“数据与入库”查看最新结果。" : "当前没有可同步的文献。";
      body.append(el("p", {class:"inline-notice", role:"status"}, message), el("div", {class:"form-actions"},
        pending ? button("查看同步任务", () => {dialog.close(); route("jobs", null, {});}) : null,
        published && recordId === null ? button("查看已入库文献", () => {dialog.close(); route("records", null, {filter:"published"});}) : null,
        close));
      return;
    }

    let busy = false;
    const list = el("div", {class:"publication-list"});
    const choices = eligible.map(row => {
      const checkbox = el("input", {type:"checkbox", checked:row.id === recordId, onchange:update});
      list.append(el("label", {class:"checkbox-label"}, checkbox,
        el("span", {}, numberTag(row), " ", row.title || row.paper_id)));
      return {id:row.id, checkbox};
    });
    const all = el("input", {type:"checkbox", onchange:() => {
      for (const {checkbox} of choices) checkbox.checked = all.checked;
      update();
    }});
    const feedback = el("div", {role:"status", "aria-live":"polite", class:"small-text"});
    const submit = button("同步所选记录", submitSelection, "primary");
    function update() {
      const count = choices.filter(item => item.checkbox.checked).length;
      all.checked = count === choices.length;
      all.indeterminate = count > 0 && count < choices.length;
      all.disabled = busy;
      for (const {checkbox} of choices) checkbox.disabled = busy;
      submit.disabled = busy || !count;
      submit.textContent = busy ? "正在提交…" : `同步所选记录（${count} 篇）`;
    }
    async function submitSelection() {
      const ids = choices.filter(item => item.checkbox.checked).map(item => item.id);
      if (busy || !ids.length) return;
      const current = new Set(publicationOptions(recordId).eligible.map(row => row.id));
      if (ids.some(id => !current.has(id))) {
        feedback.textContent = "所选文献的状态已更新。请关闭窗口后重新选择当前版本。";
        return;
      }
      busy = true;
      ids.forEach(id => publicationRequests.add(id));
      update();
      feedback.textContent = `正在核对所选的 ${ids.length} 篇文献并提交同步任务…`;
      try {
        const result = await post("/api/publish", {record_ids:ids});
        const stillOpen = dialog.open && body.isConnected;
        if (stillOpen) dialog.close();
        toast(`已提交 ${result.job_ids.length} 个图谱同步任务`);
        await perform(loadBase);
        if (stillOpen && S.page === "records") route("jobs", null, {});
      } catch (error) {
        feedback.textContent = `同步提交失败：${error.message}`;
        if (!dialog.open || !body.isConnected) toast(feedback.textContent, true);
      } finally {
        ids.forEach(id => publicationRequests.delete(id));
        busy = false;
        update();
      }
    }
    if (choices.length > 1) body.append(el("label", {class:"checkbox-label"}, all, "全选可同步文献"));
    body.append(list, feedback, el("div", {class:"form-actions"}, close, submit));
    update();
  }
  function reviews() {
    const main=$("#main");
    main.replaceChildren(pageHeader("人工抽查","只核查入选文献的指定内容，未抽中的文献无需逐篇审核。",[button("生成少量抽查清单",sampleModal,"primary","check")],"FOCUSED HUMAN REVIEW"));
    main.append(el("div",{class:"review-mode-tabs"},
      button("抽查清单",()=>route("review",null,{}),S.reviewFilter==="all"?"small primary":"small"),
      button(`已人工抽查 ${metricCount("reviewed")} 篇`,()=>route("review",null,{filter:"reviewed"}),S.reviewFilter==="reviewed"?"small primary":"small")));
    if(S.reviewFilter==="reviewed"){
      const rows=latestRecords().filter(r=>recordMatchesFilter(r,"reviewed"));
      main.append(el("div",{class:"filter-summary"},`已人工抽查 · ${rows.length} 篇当前结果；仅统计实际登记的核查，选入抽查清单不算完成核查。`),
        el("section",{class:"card"},rows.length?recordsTable(rows,"review"):empty("还没有已记录人工核查的当前结果","进入抽查清单完成指定内容核查并保存后，才会计入此数量。",button("查看抽查清单",()=>route("review",null,{}),"small"))));
      return;
    }
    main.append(el("div",{class:"sample-banner"},icon("check"),el("div",{},el("strong",{},"先核对对象与实验，再核对核心结果"),el("p",{},"每篇默认最多 6 项观测；同时可从原文主要图表反向查漏。")),button("从结果中选篇",()=>route("records"),"small")));
    if(!S.samples.length){main.append(el("section",{class:"card"},recordsTable([],"review")));return;}
    const selection=select(S.samples.map((sample,index)=>[sample.id,(index===0?"最近一批 · ":"")+shortDate(sample.created_at)+` · ${sample.record_ids.length} 篇`]),S.samples[0].id,{"aria-label":"选择抽查批次"});
    const content=el("div");
    main.append(el("div",{class:"review-list-status"},selection,el("span",{class:"pill"},"随机样本与定向样本分别展示")),content);
    function paint(){
      const sample=S.samples.find(x=>x.id===selection.value);
      const random=new Set(sample.random_ids||[]),targeted=new Set(sample.targeted_ids||[]);
      const randomRows=S.records.filter(x=>random.has(x.id)),targetedRows=S.records.filter(x=>targeted.has(x.id));
      content.replaceChildren(el("div",{class:"inline-notice"},`本批次抽样总体：${sample.population?.length??"已登记的"} 条当时满足条件的当前记录。失败、中断和未完成任务仍单列于总览与任务页，不代表这些任务已经核查。`));
      if(randomRows.length)content.append(card(`随机抽查 · ${randomRows.length} 篇`,recordsTable(randomRows,"review")));
      if(targetedRows.length)content.append(el("div",{class:"section-gap"},card(`定向抽查 · ${targetedRows.length} 篇`,el("div",{},el("div",{class:"card-body small-text muted"},sample.targeted_reason||"覆盖复杂来源或明确限制；不用于估计全库准确率。"),recordsTable(targetedRows,"review")))));
      if(!randomRows.length&&!targetedRows.length)content.append(el("section",{class:"card"},recordsTable(S.records.filter(x=>(sample.record_ids||[]).includes(x.id)),"review")));
    }
    selection.addEventListener("change",paint);paint();
  }
  function sampleModal(){const count=el("input",{class:"field-input",type:"number",min:"1",max:"30",value:Math.min(6,Math.max(1,latestRecords().length))});const random=el("input",{class:"field-input",type:"number",min:"0",max:"30",value:Math.min(4,latestRecords().length)});const body=el("div",{},el("div",{class:"form-row"},field("抽查总篇数",count),field("其中随机抽取篇数",random)),el("p",{class:"small-text muted"},"其余名额用于覆盖复杂来源或存在限制的记录。抽样会保留方法和入选范围，不代表全库准确率。"));const d=openModal("生成少量抽查清单","从已有结果中取样，不启动新的文献抽取。",body);const run=button("生成清单",()=>perform(async()=>{const n=Number(count.value),r=Number(random.value);if(!Number.isInteger(n)||!Number.isInteger(r)||n<1||r<0||r>n)throw new Error("随机篇数应在0和总篇数之间");await post("/api/sample",{count:n,random_count:r});d.close();await loadBase();route("review");await renderPage();toast("抽查清单已生成");},run),"primary");body.append(el("div",{class:"form-actions"},button("取消",()=>d.close()),run));}
  function findObs(d,id){return (d.record.observations||[]).find(o=>o.id===id);}
  function digitizationModal(d){
    const summary=d.summary||recordSummary(d.id),r=d.record;
    if(!hasResearchScope(r)){
      const body=el("div",{},el("p",{class:"inline-notice"},"旧版本保留原范围，不能自动升级补抽。请先完成新版核心结果审核；已有 JSON 和曲线点不会删除。"));
      const dlg=openModal("按需补抽曲线","没有启动模型任务。",body);
      body.append(el("div",{class:"form-actions"},button("关闭",()=>dlg.close())));return;
    }
    if(!summary.current||!summary.admissible||summary.source_change){toast("仅来源未变、审查可接纳的当前版本可提交曲线补抽。",true);return;}
    const eligible=(r.quantitative_coverage||[]).filter(s=>s.data_kind==="time_series"&&s.scope!=="external_benchmark"&&s.state!=="out_of_scope");
    const chosen=new Set(),boxes=[],list=el("div",{style:"max-height:340px;overflow:auto"});
    const count=el("p",{class:"small-text muted","aria-live":"polite"},"已选择 0 / 12 个系列");
    const question=el("textarea",{class:"field-textarea",maxlength:"8000",placeholder:"可补充所需时间范围或研究问题，例如读取反应前 20 分钟的采样点。"});
    let run;
    const update=()=>{count.textContent=`已选择 ${chosen.size} / 12 个系列`;for(const [id,box] of boxes)box.disabled=chosen.size>=12&&!chosen.has(id);if(run)run.disabled=!chosen.size||!["0.7","0.8","0.9"].includes(S.status.rules_version);};
    for(const series of eligible){
      const box=el("input",{type:"checkbox",onchange:e=>{if(e.target.checked)chosen.add(series.id);else chosen.delete(series.id);update();}});
      boxes.push([series.id,box]);
      list.append(el("label",{class:"qa-source-row"},box,el("span",{},series.label||series.id,el("small",{},`${blockLabel(series.locator||{})||series.block_id} · 已有 ${series.observation_ids?.length||0} 条记录${series.state==="complete"?" · 已声明完整，仍须核对其限制":""}`))));
    }
    const body=el("div",{},el("div",{class:"inline-notice"},"仅补抽你选中的曲线系列。核心事实和历史版本保留，读取误差与来源限制继续记录；完成后不会自动同步 Neo4j。"),
      !["0.7","0.8","0.9"].includes(S.status.rules_version)?el("p",{class:"inline-notice"},"当前网页服务尚未加载新版规则，请正常重启服务后再提交。"):null,
      eligible.length?list:el("p",{class:"small-text muted"},"本记录没有可选择的本研究时间曲线。"),count,field("补抽说明（可选）",question));
    const dlg=openModal("按需补抽曲线",`${currentModelLabel()} · 每次最多选择 12 个系列`,body);
    run=button("补抽所选系列",()=>perform(async()=>{
      if(!chosen.size||chosen.size>12)throw new Error("请选择 1—12 个系列");
      await post("/api/digitizations",{record_id:d.id,series_ids:[...chosen],question:question.value});
      dlg.close();toast("选定曲线的补抽任务已加入队列；未进行图谱写入");await loadBase();route("jobs");
    },run),"primary","spark");
    body.append(el("div",{class:"form-actions"},button("取消",()=>dlg.close()),run));update();
  }
  function evidenceIds(o){return [...new Set([...(o.evidence_ids||[]),...(o.value?.evidence_ids||[]),...(o.unit?.evidence_ids||[])])];}
  function obsInfo(d,o){const r=d.record;return {material:(r.materials||[]).find(x=>x.id===o.material_id),experiment:(r.experiments||[]).find(x=>x.id===o.experiment_id),substances:new Map((r.substances||[]).map(x=>[x.id,x]))};}
  function compactResearchTable(d,rows,decisions) {
    return el("div",{class:"table-wrap"},el("table",{class:"data-table core-data-table"},
      el("thead",{},el("tr",{},["指标 / 材料","条件、时间与原生坐标","数值 / 状态","保留理由与证据"].map(t=>el("th",{},t)))),
      el("tbody",{},rows.map(o=>{
        const info=obsInfo(d,o),decision=decisions.get(o.id)||{},ids=evidenceIds(o);
        const conditions=(info.experiment?.conditions||[]).filter(x=>x.fact?.value!=null||!["unread","not_applicable"].includes(x.fact?.status)).map(x=>`${x.name}：${fact(x.fact)} ${fact(x.unit)}`).join("；");
        return el("tr",{},el("td",{},fact(o.metric),el("small",{class:"table-subtitle"},info.material?fact(info.material.name):o.experiment_id)),
          el("td",{},el("div",{},formatTime(o.time_window)),observationCoordinates(o)?el("div",{class:"observation-coordinates"},observationCoordinates(o)):null,conditions?el("details",{class:"core-conditions"},el("summary",{},"查看本实验条件"),el("p",{},conditions)):null,el("small",{class:"table-subtitle"},(info.experiment?.targets||[]).map(t=>fact(info.substances.get(t.substance_id)?.name)).join("、"))),
          el("td",{},el("strong",{},observationValueText(o)),el("span",{class:"table-subtitle"},fact(o.unit)),el("small",{class:"tiny muted"},`${factNames[observationStatus(o)]||observationStatus(o)} · ${origins[o.origin]||o.origin}`),normalizationDetails(o)),
          el("td",{},el("span",{class:"tiny"},decision.reason||"原抽取记录保留"),ids.length?button(`${ids.length} 处依据`,()=>evidenceModal(d.id,ids[0]),"small text-button","link"):null));
      }))));
  }
  function researchLayerResults(d,view,ids) {
    const wanted=new Set(ids),all=new Map((view.observations||[]).map(x=>[x.id,x]));
    const decisions=new Map((view.decisions||[]).map(x=>[x.observation_id,x]));
    const shown=new Set(),body=el("div",{class:"core-series-list"});
    for(const series of view.series||[]){
      const rows=(series.observation_ids||[]).filter(id=>wanted.has(id)&&!shown.has(id)).map(id=>all.get(id)).filter(Boolean);
      if(!rows.length)continue;rows.forEach(o=>shown.add(o.id));
      body.append(el("details",{class:"core-series",open:series.data_kind==="cycle_series"},
        el("summary",{},el("strong",{},series.label||series.id),el("span",{class:"badge"},`${rows.length} 项`),el("span",{class:"tiny muted"},series.data_kind==="cycle_series"?"逐次循环原值":series.data_kind==="condition_series"?"逐条件原值":"同来源系列，条件按项保留")),compactResearchTable(d,rows,decisions)));
    }
    const rest=[...wanted].filter(id=>!shown.has(id)).map(id=>all.get(id)).filter(Boolean);
    const groups=new Map();for(const o of rest){if(!groups.has(o.experiment_id))groups.set(o.experiment_id,[]);groups.get(o.experiment_id).push(o);}
    for(const [experimentId,rows] of groups){const experiment=(d.record.experiments||[]).find(x=>x.id===experimentId);body.append(el("details",{class:"core-series"},el("summary",{},el("strong",{},experiment?.reported_group?.value?fact(experiment.reported_group):(purposeNames[experiment?.purpose]||experimentId)),el("span",{class:"badge"},`${rows.length} 项`)),compactResearchTable(d,rows,decisions)));}
    if(!wanted.size)body.append(el("p",{class:"small-text muted"},"此层没有独立数值观测；相关事实仍可位于材料、实验和来源索引中。"));
    return body;
  }
  function researchDesignCards(d,view) {
    const comparisons=view.comparisons||[];
    return el("details",{class:"details-section core-designs"},el("summary",{},`设计比较与适用限制（${comparisons.length}）`),
      comparisons.length?comparisons.map(c=>el("div",{class:"attachment-card"},el("strong",{},fact(c.operation)),
        el("p",{class:"small-text"},`关联材料：${(c.material_ids||[]).join("、")}；关联结果：${(c.observation_ids||[]).length} 项`),
        el("p",{class:"tiny muted"},`因果解释状态：${c.causal_status||"未确定"}`),
        ...[["保持条件",c.held_conditions],["改变条件",c.changed_conditions],["结构变化",c.structure_changes],["结果与代价",c.outcomes]].map(([name,items])=>items?.length?el("p",{class:"small-text"},name+"："+items.map(x=>x.name+" "+fact(x.fact)+" "+fact(x.unit)).join("；")):null),
        c.author_interpretation?el("p",{class:"small-text"},"作者解释："+fact(c.author_interpretation)):null,
        (c.counterevidence||[]).map(x=>el("p",{class:"small-text"},"反向证据："+fact(x))),
        (c.confounders||[]).map(x=>el("p",{class:"small-text"},"混杂因素："+fact(x))),
        (c.limitations||[]).map(x=>el("p",{class:"tiny muted"},x)),(c.evidence_ids||[]).slice(0,2).map(id=>button("原文依据",()=>evidenceModal(d.id,id),"small text-button","link")))):
        el("p",{class:"small-text muted"},"原候选未登记显式设计比较。此研究视图不会根据性能高低自动补造开发规律。"));
  }
  function researchSystemCards(d,view) {
    const selected=new Set((view.observations||[]).map(x=>x.experiment_id)),retained=new Set((view.observations||[]).map(x=>x.id));
    const substances=new Map((d.record.substances||[]).map(x=>[x.id,x])),materials=new Map((d.record.materials||[]).map(x=>[x.id,x]));
    const experiments=(d.record.experiments||[]).filter(x=>selected.has(x.id)&&["coupled","evaporation","catalysis","stability"].includes(x.purpose));
    return el("details",{class:"details-section"},el("summary",{},`污染物、催化/氧化剂与最终体系（${experiments.length} 组）`),
      experiments.map(x=>el("div",{class:"attachment-card"},el("strong",{},x.reported_group?.value?fact(x.reported_group):(purposeNames[x.purpose]||x.id)),
        el("p",{class:"small-text"},"材料："+(x.material_ids||[]).map(id=>fact(materials.get(id)?.name)+"（"+(roleNames[materials.get(id)?.role]||"角色待明确")+"）").join("、")),
        el("p",{class:"small-text"},"处理对象："+((x.targets||[]).map(t=>`${fact(substances.get(t.substance_id)?.name)} ${fact(t.concentration)} ${fact(t.unit)}；基准：${fact(t.basis)}`).join("；")||"未登记")),
        el("p",{class:"small-text"},"催化/反应投入："+((x.reaction_inputs||[]).map(i=>`${i.role} · ${fact(substances.get(i.substance_id)?.name)} · ${inputSourceNames[i.source]||i.source} · ${fact(i.amount)} ${fact(i.unit)}；基准：${fact(i.basis)}；时序：${fact(i.timing)}`).join("；")||"未单列；成品内部组分见材料卡，不根据材料名称自动推断催化位点")),
        el("p",{class:"tiny muted"},`配置：${x.configuration_id||"未绑定"}；实验：${x.id}；时间：${formatTime(x.time_window)}`),
        x.purpose==="coupled"?el("p",{class:"small-text"},"同期运行："+(jointNames[x.joint_evidence?.level]||"现有证据不足以判断")):null,
        (x.pairs||[]).map(p=>el("p",{class:"tiny muted"},`配对 ${p.left_observation_id} / ${p.right_observation_id}：${pairNames[p.level]||p.level}${retained.has(p.left_observation_id)&&retained.has(p.right_observation_id)?"":"；含归档细节，仅保留原关系，不能直接作为核心比较依据"}。${p.note||""}`)),
        (x.joint_evidence?.limitations||[]).map(t=>el("p",{class:"tiny muted"},t)))));
  }
  function researchCorePanel(d,result,showAll) {
    if(!result?.available)return el("div",{class:"card-body"},empty("此版本尚无三层研究视图",result?.reason||"研究视图尚未生成；完整抽取结果保持原样。",button("查看完整记录",showAll,"primary")));
    const view=result.view,counts=view.counts||{},provenance=view.provenance||{};
    const derived=provenance.kind==="derived_existing_candidate";
    const root=el("div",{class:"core-research-view"}),body=el("div",{class:"core-layer-body"});let selected="A";
    const history=()=>{
      const list=el("div"),dlg=openModal("研究视图版本","这些视图使用同一原始结果；切换只改变显示，不改写候选或图谱。",list);
      for(const version of result.versions||[])list.append(el("div",{class:"attachment-card"},el("p",{class:"small-text"},version.id),version.id===view.view_id?el("span",{class:"badge"},"当前显示"):button("查看此视图",()=>perform(async()=>{const next=await api(recordPath(d.id)+"/core-view?view_id="+enc(version.id));d.researchView=next;d.research_view=next;root.replaceWith(researchCorePanel(d,next,showAll));dlg.close();}),"small")));
    };
    root.append(el("div",{class:"toolbar"},el("strong",{},"围绕最终材料与设计依据"),el("div",{class:"toolbar-right"},result.versions?.length>1?button("研究视图版本",history,"small","layers"):null,button("下载研究视图",()=>window.open(recordPath(d.id)+"/core-view/download?view_id="+enc(view.view_id),"_blank","noopener"),"small","download"),button("下载完整 JSON",()=>window.open(recordPath(d.id)+"/download","_blank","noopener"),"small","download"),button("完整记录",showAll,"small"))),
      el("div",{class:"core-provenance inline-notice"},derived?`基于规则 ${provenance.source_rule_version||d.record.rule_version} 已有结果整理的研究视图。仅重组已有信息，没有重新抽取或原文审查，也不代表已经按规则 ${provenance.policy_version||"0.8"} 完成新抽取。`:`按规则 ${d.record.rule_version} 的已抽取范围生成的研究视图；显示与来源索引不等于科学验证。`,el("div",{class:"tiny"},`原版本 ${counts.source_observations??d.record.observations?.length??0} 项观测全部保留；此视图不改变原 JSON、人工核查状态或图谱版本。`)));
    const layers=el("div",{class:"core-layer-tabs",role:"tablist","aria-label":"研究内容层级"});
    const paint=()=>{
      layers.replaceChildren(...[["A","A · 核心研究",counts.core_observations,"材料、双功能结果、稳定性和设计依据"],["B","B · 场景模块",counts.module_observations,"由实际体系触发的专项内容"],["C","C · 按需来源索引",counts.deferred_observations,"原文已定位，细节按研究问题展开"]].map(([id,title,count,note])=>el("button",{type:"button",role:"tab","aria-selected":selected===id,class:"core-layer-tab"+(selected===id?" active":""),onclick:()=>{selected=id;paint();}},el("strong",{},title),el("span",{},id==="C"&&!derived?`${counts.deferred_topics||0} 个按需主题`:count==null?"来源索引":`${count} 项${derived?"已有":"已抽取"}观测`),el("small",{},note))));
      body.replaceChildren();
      if(selected==="A"){
        const finals=(d.record.materials||[]).filter(x=>["primary_final","optimized_variant","final_component"].includes(x.role));
        body.append(el("details",{class:"details-section"},el("summary",{},`最终材料、组成与制备（${finals.length}）`),el("div",{class:"record-grid"},finals.map(x=>materialCard(d,x)))),researchSystemCards(d,view),researchDesignCards(d,view),el("p",{class:"small-text muted"},"系列按原来源紧凑排列；每项结果保留自己的条件、坐标、单位、状态和证据，不合并数值或拼接最优值。"),researchLayerResults(d,view,view.core_observation_ids||[]));
      }else if(selected==="B"){
        for(const module of view.modules||[])body.append(el("div",{class:"attachment-card"},el("strong",{},module.label||module.kind||module.id),el("p",{class:"small-text"},module.reason||module.activation_reason||"见原抽取依据")));
        body.append(researchLayerResults(d,view,view.module_observation_ids||[]));
      }else{
        body.append(el("p",{class:"small-text muted"},"按需保留不表示未读或原文未报告。以下索引沿用原记录状态；旧版本已有的细节仍可在完整记录和完整 JSON 中查看。"));
        const indexes=view.source_index||[],selectedIndexes=indexes.filter(x=>(x.deferred_observation_ids||[]).length||(x.deferred_topics||[]).length);
        const list=el("div"),all=el("input",{type:"checkbox"});
        const draw=()=>{const rows=all.checked?indexes:selectedIndexes;list.replaceChildren(...rows.map(x=>el("details",{class:"core-series"},el("summary",{},el("strong",{},x.label||x.summary||x.id),el("span",{class:"badge"},`${(x.deferred_observation_ids||[]).length} 项归档细节`)),el("p",{class:"small-text"},`${x.source_id} · ${blockLabel(x.locator||{})||x.block_id}`),el("p",{class:"tiny muted"},`来源状态：${x.reading_state||x.state||"未指定"}；保留 ${(x.retained_observation_ids||[]).length} 项关键结果`),x.reason?el("p",{class:"small-text"},x.reason):null,(x.deferred_topics||[]).length?el("p",{class:"small-text"},"按需主题："+x.deferred_topics.join("、")):null,(x.limitations||[]).map(t=>el("p",{class:"tiny muted"},t)))));if(!rows.length)list.append(el("p",{class:"small-text muted"},"此版本没有登记待展开的来源条目。"));};
        all.addEventListener("change",draw);body.append(el("label",{class:"checkbox-label"},all,`显示全部 ${indexes.length} 项来源索引`),list);draw();
      }
      if(view.eligibility?.length){
        const uses={material_retrieval:"材料检索与溯源",condition_comparison:"条件比较",performance_pairing:"双功能配对比较",composition_features:"组成特征分析",design_hypothesis:"材料设计假设"};
        const states={eligible:"模型判断具备所列使用依据",limited:"限于所列范围",not_applicable:"不适用",not_assessed:"尚未评估"};
        body.append(el("details",{class:"details-section"},el("summary",{},"下游用途与限制"),view.eligibility.map(x=>el("div",{class:"attachment-card"},el("strong",{},`${uses[x.use]||x.use} · ${states[x.status]||x.status}`),el("p",{class:"small-text"},x.reason),el("p",{class:"tiny muted"},`${x.target_kind}：${x.target_id}`),(x.limitations||[]).map(t=>el("p",{class:"tiny muted"},t))))));
      }
      if(view.limitations?.length)body.append(el("details",{class:"details-section"},el("summary",{},"此视图的使用限制"),el("ul",{class:"small-text"},view.limitations.map(x=>el("li",{},x)))));
    };
    root.append(layers,body,el("div",{class:"core-view-identity tiny muted"},`研究视图版本：${view.view_id} · 原始结果指纹：${String(provenance.source_record_sha256||result.record_hash||"").slice(0,16)}…`));paint();return root;
  }
  async function recordView(id,reviewMode=false){const m=$("#main");m.replaceChildren(loading("正在加载结果、原文与证据…"));try{const d=await detail(id);if(!reviewMode){try{d.researchView=d.research_view||await api(recordPath(id)+"/core-view");}catch(error){d.researchView={available:false,reason:"研究视图校验未通过："+error.message};}}if(!location.hash.includes(enc(id)))return;const r=d.record;const summary={...recordSummary(id),...(d.summary||{})};const top=el("div",{class:"record-top"},el("button",{type:"button",class:"record-back","aria-label":"返回列表",onclick:()=>route(reviewMode?"review":"records")},"←"),el("div",{class:"record-top-main"},el("div",{class:"record-title-identity"},numberTag({...summary,paper_id:r.paper.id})),el("h2",{},fact(r.paper.title)),el("div",{class:"record-info"},el("span",{},fact(r.paper.doi)),recordProcessingBadge({...summary,status:summary.status||d.result?.status||"candidate_unverified"}),badge(summary.publish_status||"unpublished"))),el("div",{class:"record-top-actions"},reviewMode?button("查看全部结果",()=>route("records",id),"small"):button("人工抽查",()=>route("review",id),"small","check"),button("向本文提问",()=>{S.qaSelected=new Set([id]);route("qa");},"small","chat")));const panel=el("section",{class:"record-panel"},top);m.replaceChildren(panel);if(reviewMode){if(summary.current===false){const latest=latestRecords().find(x=>recordPaperId(x)===recordPaperId(summary));panel.append(el("div",{class:"card-body"},empty("此版本已保留为历史","历史内容只供查看。新的人工核查应提交到当前版本，避免覆盖后续修订。",latest?button("打开当前版本",()=>route("review",latest.id),"primary"):null)));return;}if(!S.review.has(d.id))d.draft=await api(recordPath(d.id)+"/draft?reviewer="+enc(reviewerName()));await reviewWorkspace(d,panel);return;}let tab="core";const tabs=el("div",{class:"tab-strip"});const content=el("div");const paint=()=>{tabs.replaceChildren(...[["core","核心研究视图"],["results","完整记录"],["materials","最终材料"],["audit","审查与限制"],["history","版本与核查"]].map(([v,t])=>el("button",{type:"button",class:"tab-button"+(tab===v?" active":""),onclick:()=>{tab=v;paint();}},t)));content.replaceChildren();if(tab==="core"){content.append(researchCorePanel(d,d.researchView,()=>{tab="results";paint();}));}else if(tab==="results"){
      const selectPurpose=select([["performance","性能与稳定性"],["coupled","蒸发与催化联合"],["stability","稳定性"],["evaporation","蒸发"],["catalysis","催化"],["all","全部指标（含表征、机理）"]],"performance",{"aria-label":"实验与指标范围"});
      const controls=el("input",{type:"checkbox"}),archived=el("input",{type:"checkbox"}),source=el("div"),scope=el("div",{class:"result-scope-summary"});
      const scoped=hasResearchScope(r),archivedCount=scoped?(r.observations||[]).filter(o=>o.analysis_role==="dense_series_point").length:0;
      const materialRoles=new Map((r.materials||[]).map(x=>[x.id,x.role]));
      const experiments=new Map((r.experiments||[]).map(x=>[x.id,x]));
      const roleOrder={primary_final:0,optimized_variant:1,final_component:2,active_component:3,necessary_control:4,precursor:5};
      const isFinal=materialId=>["primary_final","optimized_variant","final_component"].includes(materialRoles.get(materialId));
      const belongsToFinal=o=>o.material_id?isFinal(o.material_id):(experiments.get(o.experiment_id)?.material_ids||[]).some(isFinal);
      const rank=o=>o.material_id?(roleOrder[materialRoles.get(o.material_id)]??8):belongsToFinal(o)?2:9;
      const draw=()=>{
        const observations=r.observations||[];
        const rows=visibleResearchObservations(r,archived.checked).filter(o=>controls.checked||belongsToFinal(o)).filter(o=>{
          const purpose=experiments.get(o.experiment_id)?.purpose;
          return selectPurpose.value==="all"||(selectPurpose.value==="performance"?["coupled","stability","evaporation","catalysis"].includes(purpose):purpose===selectPurpose.value);
        }).sort((a,b)=>rank(a)-rank(b)||(a.importance==="core"?0:1)-(b.importance==="core"?0:1));
        scope.textContent=`显示 ${rows.length} / ${observations.length} 项观测 · ${controls.checked?"包含对照、前驱体及其他对象":"仅最终材料、变体及其明确关联的实验"} · ${selectPurpose.options[selectPurpose.selectedIndex].text}${scoped?` · ${archivedCount} 条曲线点保留在完整档案；${archived.checked?"已显示归档点":"默认显示研究结果"}`:" · 旧版本保留原抽取范围"}`;
        source.replaceChildren(observationTable(d,rows));
      };
      selectPurpose.addEventListener("change",draw);controls.addEventListener("change",draw);archived.addEventListener("change",draw);
      content.append(el("div",{class:"toolbar"},selectPurpose,el("label",{class:"checkbox-label"},controls,"显示对照与其他材料"),scoped?el("label",{class:"checkbox-label"},archived,"显示已归档曲线点"):null,el("div",{class:"toolbar-right"},button("按需补抽曲线",()=>digitizationModal(d),"small","spark"),button("下载 JSON",()=>window.open(recordPath(id)+"/download","_blank","noopener"),"small","download"),button("同步到图谱",()=>publishModal(id),"small","database"))),el("div",{class:"small-text muted",style:"padding:0 20px 12px"},scoped?"核心结果与归档曲线点分别显示。下载 JSON 始终包含此版本的全部数据；逐点补抽须单独选择系列。":"此历史规则版本的数据不自动删减或重新分类；曲线补抽需要先完成新版核心结果审核。"),scope,source);draw();
    }else if(tab==="materials"){const materials=r.materials||[];const finals=materials.filter(x=>["primary_final","final_component","optimized_variant"].includes(x.role));const body=el("div",{class:"record-grid"},finals.map(x=>materialCard(d,x)));const rest=materials.filter(x=>!finals.includes(x));if(rest.length)body.append(el("details",{class:"details-section"},el("summary",{},`必要对照、前驱体与辅助材料（${rest.length}）`),rest.map(x=>materialCard(d,x))));content.append(body);}else if(tab==="audit"){const audit=d.audit||{};const items=[...(audit.coverage_limitations||[]),...(audit.potential_omissions||[])];content.append(el("div",{class:"card-body"},quantitativeCoverageCard(d),quantitativeFollowupCard(d),audit.review_scope==="citation_delta"?el("div",{class:"inline-notice"},"本次仅复核证据引用变更及关联范围，其余内容继承此前已绑定的完整审查；这不是全篇重新审查，也不代表人工验证。"):null,el("div",{class:"inline-notice"},"自动审查状态与来源限制分别记录。已报告的冲突、缺失或未核查范围不会因机器校验通过而消失。"),items.map(x=>el("div",{class:"attachment-reason"},x)),(audit.findings||[]).map(f=>el("div",{class:"attachment-card"},el("div",{class:"badges"},badge(f.verdict),f.needs_revision?badge("needs_recheck"):null),el("p",{class:"small-text"},f.explanation),el("div",{class:"mono muted"},f.record_path||"")))));}else{const history=d.history||[];content.append(el("div",{class:"card-body"},el("h3",{class:"small-text"},"版本记录"),history.length?el("ul",{class:"history-list"},history.map(h=>el("li",{},button("查看此版本",()=>route("records",h.id||h.record_id),"small text-button"),el("small",{},shortDate(h.created_at)),h.parent_id?button("与上一版本比较",()=>versionDiffModal(h.parent_id,h.id),"small text-button"):null,h.status?badge(h.status):null))):el("p",{class:"small-text muted"},"当前为已登记的抽取版本。"),el("h3",{class:"small-text"},"已提交的人工核查"),(d.reviews||[]).length?(d.reviews||[]).map(a=>el("div",{class:"attachment-card"},el("div",{class:"small-text"},a.reviewer||"审核记录"),el("div",{class:"tiny muted"},shortDate(a.created_at)),el("p",{class:"small-text"},`${a.entries?.length??a.payload?.entries?.length??0} 项记录；仅适用于明确核查范围。`))):el("p",{class:"small-text muted"},"尚无人提交核查。查看过原文或结果不会自动标为人工已审核。")));}};panel.append(tabs,content);paint();}catch(e){m.replaceChildren(errorPanel(e,()=>recordView(id,reviewMode)));}}
  function materialCard(d,material) {
    const substances=new Map((d.record.substances||[]).map(x=>[x.id,x]));
    const axisNames={composition:"组成",chemical_family:"化学类别",chemical_class:"化学类别",morphology:"形貌",architecture:"结构",functional_role:"功能角色",source:"来源",source_class:"来源"};
    const card=el("div",{class:"material-card"},el("div",{class:"badges"},badge(material.role)),el("h3",{},fact(material.name)),el("div",{class:"badges"},(material.classification||[]).map(c=>el("span",{class:"badge"},(axisNames[c.axis]?axisNames[c.axis]+" · ":"")+fact(c.label)))),el("div",{class:"small-text muted"},(material.morphology||[]).map(fact).join("；")||"未给出可展示的形貌描述"));
    if(material.components?.length){
      const components=el("div",{class:"material-composition"},el("h4",{},"组成与功能"));
      const retentionNames={retained:"保留在成品中",preloaded_consumable:"预装耗材",removed_or_transformed:"已去除或转化",analytical_only:"仅用于分析",unknown:"成品保留状态未确定"};
      components.append(el("p",{class:"tiny muted"},"记录量按原始计量基准使用；投料、浴液或负载操作用量不能自动视为成品实测含量。"));
      for(const c of material.components)components.append(el("div",{class:"component-row"},el("strong",{},fact(substances.get(c.substance_id)?.name)),el("span",{},(c.roles||[]).map(fact).join("、")||"功能角色未明确"),el("small",{},`记录量：${fact(c.amount)} ${fact(c.unit)}；计量基准：${fact(c.basis)}`),el("small",{},`成品状态：${retentionNames[c.retention]||c.retention||"未确定"}`),c.position?.value!=null||c.position?.status&&c.position.status!=="unread"?el("small",{},"位置："+fact(c.position)):null));
      card.append(components);
    }
    if(material.preparation?.length)card.append(el("details",{class:"details-section"},el("summary",{},"制备与结构设计路径"),el("ol",{class:"preparation-list"},[...material.preparation].sort((a,b)=>a.order-b.order).map(step=>el("li",{},fact(step.operation),el("div",{class:"tiny muted"},(step.parameters||[]).map(x=>x.name+"："+fact(x.fact)+" "+fact(x.unit)).join("；")))))));
    card.append(...(material.evidence_ids||[]).slice(0,2).map(e=>button("查看原文依据",()=>evidenceModal(d.id,e),"small text-button","link")));
    return card;
  }
  async function versionDiffModal(beforeId,afterId) {
    openModal("记录版本比较","按事实字段显示变化，历史原值保持不变。",loading());
    const body=$("#modal-content .modal-body");
    try{
      const [before,after]=await Promise.all([detail(beforeId),detail(afterId)]);
      const oldObs=new Map((before.record.observations||[]).map(x=>[x.id,x]));
      const changes=[];
      for(const observation of after.record.observations||[]){
        const previous=oldObs.get(observation.id);
        if(!previous){changes.push({observation,field:"新增观测",old:"没有该项",next:observationValueText(observation)+" "+fact(observation.unit)});continue;}
        for(const [key,label] of [["value","数值与状态"],["unit","单位"],["metric","指标"],["origin","数值与结论来源"],["material_id","材料归属"],["experiment_id","实验归属"],["time_window","时间窗口"],["series_id","曲线 / 数据系列"],["coordinates","数据点坐标"],["evidence_ids","证据绑定"],["evidence_links","逐字段证据支持"]]){
          if(JSON.stringify(previous[key])===JSON.stringify(observation[key]))continue;
          const show=item=>{
            const value=item[key];
            if(key==="value")return observationValueText(item)+(observationNormalizationText(item)?"；另存换算："+observationNormalizationText(item):"")+"（"+(factNames[observationStatus(item)]||observationStatus(item))+"）"+(observationStatus(item)!==value?.status?"；原始字段状态："+(factNames[value?.status]||value?.status):"");
            if(key==="time_window")return formatTime(value);
            if(key==="origin")return origins[value]||value||"来源待明确";
            if(key==="coordinates")return observationCoordinates(item)||"未登记坐标";
            if(key==="evidence_links")return [...new Set((value||[]).map(link=>link.evidence_id))].map(eid=>eid+"："+evidenceUseText(item,eid)).join("\n")||"未登记逐字段支持";
            if(value&&typeof value==="object"&&"status" in value)return fact(value)+"（"+(factNames[value.status]||value.status)+"）"+(value.normalized!=null?"；另存规范化："+fact({value:value.normalized,status:value.status})+"；说明："+(value.normalization_reason||"未登记"):"");
            return Array.isArray(value)?value.join("、"):String(value??"未绑定");
          };
          changes.push({observation,field:label,old:show(previous),next:show(observation)});
        }
        oldObs.delete(observation.id);
      }
      for(const observation of oldObs.values())changes.push({observation,field:"当前版本不再包含",old:observationValueText(observation)+" "+fact(observation.unit),next:"本版本无该项，历史保留"});
      const wrap=el("div",{},el("p",{class:"small-text muted"},"此比较列出数值、来源类型、实验归属、系列坐标及证据关联的变化；图表覆盖清单、材料分类和完整结构可通过各版本 JSON 查看。"));
      if(!changes.length)wrap.append(empty("所列观测字段没有变化","图表覆盖清单、材料分类、审核状态等其他内容仍可能变化，请结合各版本 JSON 查看。"));
      for(const change of changes)wrap.append(el("div",{class:"attachment-card"},el("strong",{class:"small-text"},fact(change.observation.metric)+" · "+change.field),el("div",{class:"version-diff-row"},el("span",{},"原版本"),el("div",{},change.old)),el("div",{class:"version-diff-row new"},el("span",{},"新版本"),el("div",{},change.next))));
      body.replaceChildren(wrap);
    }catch(error){body.replaceChildren(errorPanel(error));}
  }
  function observationTable(d,rows){if(!rows.length)return empty("没有对应的实验结果","当前筛选下没有观测记录。");return el("div",{class:"table-wrap"},el("table",{class:"data-table"},el("thead",{},el("tr",{},["材料 / 实验","指标","数值与单位","时间 / 处理对象","证据"].map(t=>el("th",{},t)))),el("tbody",{},rows.map(o=>{const info=obsInfo(d,o),eids=evidenceIds(o);return el("tr",{},el("td",{},el("span",{class:"table-title"},info.material?fact(info.material.name):"见实验材料"),el("div",{class:"table-subtitle"},purposeNames[info.experiment?.purpose]||o.experiment_id)),el("td",{},fact(o.metric),el("div",{class:"table-subtitle"},origins[o.origin]||o.origin),hasResearchScope(d.record)?el("div",{class:"tiny muted"},analysisRoleNames[o.analysis_role]||"用途尚未分类"):null),el("td",{},el("button",{type:"button",class:"metric-link",onclick:()=>{if(eids[0])evidenceModal(d.id,eids[0]);else toast("该项未提供可定位的证据",true);}},observationValueText(o)),estimateBadge(o),el("div",{class:"table-subtitle"},fact(o.unit)),normalizationDetails(o)),el("td",{},formatTime(o.time_window),observationCoordinates(o)?el("div",{class:"observation-coordinates"},observationCoordinates(o)):null,el("div",{class:"table-subtitle"},(info.experiment?.targets||[]).map(t=>fact(info.substances.get(t.substance_id)?.name)).join("、")||"未指定处理对象")),el("td",{},eids.length?button(`${eids.length} 处依据`,()=>evidenceModal(d.id,eids[0]),"small text-button","link"):el("span",{class:"tiny muted"},"未提供")));}))),el("div",{class:"observations-count"},`当前显示 ${rows.length} 项观测；点击数值可定位证据。`));}
  function formatTime(w){if(!w)return "时间未给出";const parts=[];if(w.start?.value!=null)parts.push(fact(w.start));if(w.end?.value!=null)parts.push(fact(w.end));return parts.length?parts.join("–")+" "+fact(w.unit):(w.reference?.value?fact(w.reference):"时间未报告 / 待明确");}
  function sourceViewer(d) {
    const host=el("div",{class:"source-pane"});
    const toolbar=el("div",{class:"source-toolbar"});
    const viewer=el("div",{class:"source-viewer"},el("div",{class:"source-empty"},"选择一项结果证据，或从上方切换正文与 SI。"));
    const locator=el("div",{class:"source-title-bar"},"按原文物理页或 Word 结构块定位");
    const context=el("details",{class:"source-context"});
    const sources=d.sources||[];
    const choice=select(sources.map(x=>[x.id,(roleNames[x.role]||x.role||"来源")+" · "+x.name]),sources[0]?.id||"",{"aria-label":"选择正文或SI"});
    const pageInput=el("input",{type:"number",min:1,value:1,"aria-label":"PDF物理页"});
    const pageLabel=el("span",{},"页"), zoomLabel=el("span",{class:"tiny"},"100%");
    let source=null,data=null,serial=0,zoom=1,currentPage=1,lastEvidence=null;
    const open=button("原件 ↗",()=>{const url=safeUrl(source?.file_url);if(url)window.open(url,"_blank","noopener");},"small text-button");
    const prev=button("‹",()=>showPdf(currentPage-1),"small text-button");
    const next=button("›",()=>showPdf(currentPage+1),"small text-button");
    prev.setAttribute("aria-label","上一物理页");next.setAttribute("aria-label","下一物理页");
    const minus=button("−",()=>setZoom(zoom-.25),"small text-button");
    const plus=button("＋",()=>setZoom(zoom+.25),"small text-button");
    minus.setAttribute("aria-label","缩小原文");plus.setAttribute("aria-label","放大原文");
    const full=button("展开",()=>{if(document.fullscreenElement){document.exitFullscreen().catch(e=>toast(e.message,true));}else if(host.requestFullscreen){host.requestFullscreen().catch(e=>toast(e.message,true));}else toast("当前浏览器不支持全屏，请使用原件入口");},"small text-button");
    toolbar.append(choice,prev,pageInput,pageLabel,next,minus,zoomLabel,plus,full,open);
    host.append(toolbar,locator,context,viewer);
    function setZoom(value){zoom=Math.min(3,Math.max(.5,value));zoomLabel.textContent=Math.round(zoom*100)+"%";if(source?.format==="pdf")showPdf(currentPage);else for(const image of viewer.querySelectorAll("img")){image.style.maxWidth="none";image.style.width=(zoom*100)+"%";}}
    function showPdf(value,preview) {
      if (!source) return;
      currentPage=Math.max(1,Math.min(Number(source.pages)||9999,Math.round(Number(value)||1)));
      pageInput.value=currentPage;
      prev.disabled=currentPage<=1;next.disabled=!!source.pages&&currentPage>=source.pages;
      viewer.classList.remove("pdf-mode");
      const template=source.page_template;
      let url=safeUrl(template?template.replace("{page}",currentPage):preview||data?.previews?.find(x=>Number(x.page)===currentPage)?.url);
      if (!url && currentPage===1) url=safeUrl(source.preview_url);
      locator.textContent=`${roleNames[source.role]||"原文"} · 物理页 ${currentPage}${source.pages?" / "+source.pages:""} · 页级定位，未标示精确文字坐标`;
      if(url){
        const renderUrl=new URL(url);if(template)renderUrl.searchParams.set("scale",String(Math.min(4,Math.max(1,zoom*2))));
        const img=el("img",{src:renderUrl.href,alt:`${source.name}，物理页 ${currentPage}`,style:`width:${zoom*100}%;max-width:none`});
        const ticket=serial;img.addEventListener("error",()=>{if(ticket===serial&&img.isConnected)viewer.replaceChildren(errorPanel("此页图像暂时无法读取，可重试或打开原件。",()=>showPdf(currentPage)));});
        viewer.replaceChildren(img);
      }else{
        const file=safeUrl(source.file_url);
        viewer.classList.add("pdf-mode");
        viewer.replaceChildren(file?el("iframe",{src:file+"#page="+currentPage+"&view=FitH",title:"文献PDF原件"}):empty("没有可用的页图","使用原件入口查看该来源。"));
      }
    }
    function showContext(ev){
      context.replaceChildren();context.hidden=!ev;
      if(!ev)return;
      context.append(el("summary",{},"引用片段与前后文"));
      if(ev.quote)context.append(el("div",{class:"source-quote"},el("strong",{},"抽取记录中的引用"),el("p",{},ev.quote)));
      for(const block of ev.context||[])context.append(el("div",{class:"source-context-block"},el("small",{},blockLabel(block.location||{})||block.kind||"结构块"),el("p",{},block.text||"该结构块为图像，见下方原文。")));
    }
    async function load(sourceId,ev=null) {
      const ticket=++serial;
      source=sources.find(x=>x.id===sourceId)||sources[0];lastEvidence=ev;
      if(!source){viewer.replaceChildren(empty("没有可用来源","此记录没有绑定可阅读的原文。"));return;}
      choice.value=source.id;
      const pdf=source.format==="pdf";
      for(const n of [prev,next,pageInput,pageLabel])n.hidden=!pdf;
      open.disabled=!safeUrl(source.file_url);
      viewer.replaceChildren(loading("正在定位原文…"));
      showContext(ev);
      try{
        const loaded=await api(recordPath(d.id)+"/sources/"+enc(source.id));
        if(ticket!==serial)return;
        data=loaded;
        source={...source,...data.source};
        zoom=1;zoomLabel.textContent="100%";
        if(pdf){pageInput.max=source.pages||9999;showPdf(ev?.page||ev?.location?.physical_page||1,ev?.preview_url);return;}
        locator.textContent="Word 原生结构 · 按段落、表格与图片实例定位，不代表完整页面排版";
        viewer.classList.remove("pdf-mode");
        const fragments=[],renderedImages=new Set();
        for(const block of data.blocks||[]){
          const id=block.block_id||block.id;
          const item=el("div",{class:"source-block"+(id===ev?.block_id?" highlight":""),id:"block-"+id},el("small",{},blockLabel(block.location||{})||block.kind||"结构块"),block.text||"");
          fragments.push(item);
          for(const preview of (data.previews||[]).filter(x=>x.block_id===id)){
            const url=safeUrl(preview.url);if(url){fragments.push(el("img",{src:url,alt:"Word 中的原始图片实例",loading:"lazy"}));renderedImages.add(preview.url);}
          }
        }
        for(const preview of data.previews||[]){const url=safeUrl(preview.url);if(url&&!renderedImages.has(preview.url))fragments.push(el("img",{src:url,alt:"Word 附件图片实例",loading:"lazy"}));}
        if(!fragments.length)fragments.push(empty("暂无可显示的结构块","原格式文件仍被保留，可打开原件查看。"));
        viewer.replaceChildren(...fragments);
        if(ev?.block_id){const block=Array.from(viewer.querySelectorAll(".source-block")).find(x=>x.id==="block-"+ev.block_id);if(block)block.scrollIntoView({block:"center"});}
      }catch(error){if(ticket===serial)viewer.replaceChildren(errorPanel(error,()=>load(source.id,lastEvidence)));}
    }
    choice.addEventListener("change",()=>load(choice.value));pageInput.addEventListener("change",()=>showPdf(pageInput.value));
    context.hidden=true;return{host,load,viewer};
  }
  function blockLabel(l){const parts=[];if(l.physical_page)parts.push("物理页 "+l.physical_page);if(l.printed_page)parts.push("印刷页 "+l.printed_page);if(l.paragraph_index)parts.push("段落 "+l.paragraph_index);if(l.table_index)parts.push("表格 "+l.table_index);if(l.row_index)parts.push("行 "+l.row_index);if(l.cell_index)parts.push("单元格 "+l.cell_index);if(l.figure)parts.push("图 "+l.figure);if(l.panel)parts.push("面板 "+l.panel);return parts.join(" · ");}
  async function evidenceModal(recordId,evidenceId){const dialog=$("#evidence-dialog");const box=$("#evidence-dialog-content");box.replaceChildren(el("div",{class:"modal-head"},el("div",{},el("h2",{},"原文证据"),el("p",{},"查看证据不等于已经提交人工核查。")),el("button",{class:"close-button",type:"button","aria-label":"关闭证据",onclick:()=>dialog.close()},"×")),el("div",{class:"modal-body"},loading()));if(!dialog.open)dialog.showModal();try{const [d,e]=await Promise.all([detail(recordId),api(recordPath(recordId)+"/evidence/"+enc(evidenceId))]);const sv=sourceViewer(d);sv.host.style.width="100%";sv.host.style.height="100%";box.querySelector(".modal-body").replaceChildren(sv.host);await sv.load(e.source_id,e);}catch(e){box.querySelector(".modal-body").replaceChildren(errorPanel(e));}}
  function getReviewState(d){if(!S.review.has(d.id)){const saved=d.draft||{};S.review.set(d.id,{reviewer:saved.reviewer||reviewerName(),base_hash:saved.base_hash||d.summary?.record_hash||recordSummary(d.id).record_hash||d.record_hash||"",entries:new Map((saved.entries||[]).map(e=>[e.observation_id,e])),timer:null,saving:false,dirty:false,revision:0,status:(saved.entries||[]).length?"已恢复草稿 · 尚未提交":"尚未提交"});}return S.review.get(d.id);}
  async function saveDraft(d,rs,statusLabel) {
    if(!rs.dirty||rs.saving)return;
    const revision=rs.revision;
    rs.saving=true;rs.status="正在保存草稿…";if(statusLabel)statusLabel.textContent=rs.status;
    let succeeded=false;
    try{await post(recordPath(d.id)+"/draft",{reviewer:rs.reviewer,base_hash:rs.base_hash,entries:structuredClone([...rs.entries.values()])});succeeded=true;rs.dirty=rs.revision!==revision;rs.status=rs.dirty?"草稿有新改动":"草稿已保存 · 尚未提交";}
    catch(error){rs.status="草稿保存失败 · 可重试";toast(error.message,true);}
    finally{rs.saving=false;if(statusLabel)statusLabel.textContent=rs.status;if(succeeded&&rs.dirty){clearTimeout(rs.timer);rs.timer=setTimeout(()=>saveDraft(d,rs,statusLabel),650);}}
  }
  async function reviewWorkspace(d,panel){const rs=getReviewState(d);const obs=d.record.observations||[];const selected=d.selected_observation_ids?.length?d.selected_observation_ids:obs.slice(0,6).map(o=>o.id);let ids=[...selected];let index=Math.max(0,ids.indexOf(S.activeObservation.get(d.id)));let tab="core";const source=sourceViewer(d);const right=el("div",{class:"result-pane"});const tabs=el("div",{class:"tab-strip"});const scroll=el("div",{class:"result-scroll"});const statusLabel=el("span",{class:"review-save-status"},rs.status);const bottom=el("div",{class:"review-bottom"});right.append(tabs,scroll,bottom);const grip=el("div",{class:"split-handle",role:"separator","aria-label":"调整原文与结果区域宽度","aria-orientation":"vertical",tabindex:0});const layout=el("div",{class:"review-layout"},source.host,grip,right);panel.append(layout);let dragging=false;grip.addEventListener("pointerdown",e=>{dragging=true;grip.setPointerCapture(e.pointerId);});grip.addEventListener("pointermove",e=>{if(!dragging||window.innerWidth<=900)return;const rect=layout.getBoundingClientRect();const pct=Math.max(32,Math.min(69,(e.clientX-rect.left)/rect.width*100));source.host.style.width=pct+"%";});grip.addEventListener("pointerup",()=>dragging=false);grip.addEventListener("keydown",e=>{if(["ArrowLeft","ArrowRight"].includes(e.key)){e.preventDefault();const pct=parseFloat(source.host.style.width)||56;source.host.style.width=Math.max(32,Math.min(69,pct+(e.key==="ArrowRight"?3:-3)))+"%";}});
    const schedule=()=>{rs.dirty=true;rs.revision++;rs.status="草稿有改动";statusLabel.textContent=rs.status;clearTimeout(rs.timer);rs.timer=setTimeout(()=>saveDraft(d,rs,statusLabel),650);};
    let evidenceRequest=0;async function showEvidence(eid){const ticket=++evidenceRequest;try{const ev=await api(recordPath(d.id)+"/evidence/"+enc(eid));if(ticket!==evidenceRequest)return;await source.load(ev.source_id,ev);}catch(e){toast(e.message,true);}}
    function navigation(){const prev=button("←",()=>{if(index>0){index--;paint();}},"small");prev.disabled=index===0;const next=button("→",()=>{if(index<ids.length-1){index++;paint();}},"small");next.disabled=index>=ids.length-1;const submit=button("提交本次核查",()=>submitReview(d,rs),"small primary");const save=button("保存草稿",()=>saveDraft(d,rs,statusLabel),"small text-button");bottom.replaceChildren(el("div",{},statusLabel,el("div",{class:"tiny muted"},"仅记录实际勾选的核查范围"),save),el("div",{class:"result-pagination"},prev,el("span",{},ids.length?`${index+1} / ${ids.length}`:"0 / 0"),next,submit));}
    function setEntry(id,change){const current=rs.entries.get(id)||{observation_id:id,action:"uncertain",scope:["value","unit"],patch:{},note:"",evidence_ids:evidenceIds(findObs(d,id)||{})};Object.assign(current,change);rs.entries.set(id,current);schedule();}
    function paint(){tabs.replaceChildren(...[["core","核心结果"],["stability","稳定性"],["material","材料概览"],["issues","原文冲突"],["omission","反向查漏"]].map(([v,t])=>el("button",{type:"button",class:"tab-button"+(tab===v?" active":""),onclick:()=>{tab=v;if(v==="core")ids=[...selected];if(v==="stability")ids=obs.filter(o=>(d.record.experiments||[]).find(e=>e.id===o.experiment_id)?.purpose==="stability").slice(0,6).map(o=>o.id);index=0;paint();}},t)));scroll.replaceChildren();navigation();if(tab==="material"){scroll.append(...(d.record.materials||[]).filter(m=>["primary_final","final_component","optimized_variant"].includes(m.role)).map(m=>materialCard(d,m)));return;}if(tab==="issues"){const audit=d.audit||{};const findings=(audit.findings||[]).filter(f=>f.verdict!=="supported");scroll.append(el("div",{class:"inline-notice"},"已如实记录的原文冲突不需要强行选择一个数值。"),...(audit.coverage_limitations||[]).map(t=>el("div",{class:"attachment-reason"},t)),...findings.map(f=>el("div",{class:"attachment-card"},badge(f.verdict),el("p",{class:"small-text"},f.explanation),el("div",{class:"tiny muted"},f.record_path||""))));if(!findings.length)scroll.append(el("p",{class:"small-text muted"},"当前模型审查没有列出此类问题。"));return;}if(tab==="omission"){renderOmission();return;}const o=findObs(d,ids[index]);if(!o){scroll.append(empty("没有对应的抽查结果","可以查看全部结果或切换其他核查主题。"));return;}S.activeObservation.set(d.id,o.id);const info=obsInfo(d,o);const eids=evidenceIds(o);scroll.append(el("div",{class:"result-overline"},el("span",{},`${tab==="stability"?"稳定性":"核心结果"} · 观测 ${index+1}`),badge(observationStatus(o))),el("div",{class:"material-chip"},info.material?fact(info.material.name):"材料归属见实验"),el("h3",{class:"metric-name"},fact(o.metric)),el("div",{class:"metric-value"},observationValueText(o),el("span",{class:"metric-unit"},fact(o.unit))),el("div",{class:"badges"},badge(origins[o.origin]||o.origin)),normalizationDetails(o));const conditions=el("div",{class:"conditions"});const row=(name,value)=>conditions.append(el("div",{class:"condition-row"},el("span",{class:"condition-name"},name),el("span",{class:"condition-value"},value)));row("实验",purposeNames[info.experiment?.purpose]||o.experiment_id);row("处理对象",(info.experiment?.targets||[]).map(t=>`${fact(info.substances.get(t.substance_id)?.name)} · ${fact(t.concentration)} ${fact(t.unit)}`).join("；")||"未指定 / 不适用");row("反应投入",(info.experiment?.reaction_inputs||[]).map(t=>`${t.substance_id?fact(info.substances.get(t.substance_id)?.name):t.role} · ${inputSourceNames[t.source]||t.source} · ${fact(t.amount)} ${fact(t.unit)}`).join("；")||"原文未报告 / 未登记");row("能量条件",(info.experiment?.energy_inputs||[]).map(t=>`${t.kind} ${(t.parameters||[]).map(p=>p.name+" "+fact(p.fact)+" "+fact(p.unit)).join("；")}`).join("；")||"未报告 / 未登记");row("观测时间",formatTime(o.time_window));if(observationCoordinates(o))row("数据点坐标",observationCoordinates(o));if(o.series_id)row("图表系列",o.series_id);for(const condition of info.experiment?.conditions||[])row(condition.name,fact(condition.fact)+" "+fact(condition.unit));if(o.stream_id){const configuration=(d.record.configurations||[]).find(x=>x.id===info.experiment?.configuration_id);const stream=configuration?.streams?.find(x=>x.id===o.stream_id);row("采样相",stream?(streamNames[stream.kind]||stream.kind)+" · "+fact(stream.description):"采样相见实验装置定义");}const pairs=(info.experiment?.pairs||[]).filter(p=>p.left_observation_id===o.id||p.right_observation_id===o.id);row("同期运行",jointNames[info.experiment?.joint_evidence?.level]||"证据待明确");for(const p of pairs)row("性能配对",(pairNames[p.level]||"关系待明确")+" · "+(p.note||(p.matched_conditions||[]).join("；")||"查看配对证据"));scroll.append(conditions);for(const eid of eids.slice(0,3)){const ev=(d.record.evidence||[]).find(x=>x.id===eid);const loc=blockLabel(ev?.locator||{});scroll.append(el("div",{class:"evidence-quote"},el("p",{},ev?.quote||"此证据提供来源定位，点击查看原文上下文。"),el("div",{class:"evidence-use"},evidenceUseText(o,eid)),el("footer",{},el("span",{},loc||"来源结构块"),button("定位原文 ↗",()=>showEvidence(eid),"small text-button"))));}if(eids[0])showEvidence(eids[0]);if(o.limitations?.length)scroll.append(el("details",{class:"details-section"},el("summary",{},"此项使用限制"),el("ul",{},o.limitations.map(t=>el("li",{},t)))));renderReviewForm(o);}
    function renderReviewForm(o) {
      const saved=rs.entries.get(o.id),form=el("div",{class:"review-form"},el("h3",{},"本次核查什么"));
      const scopes=new Set(saved?.scope||["value","unit"]);
      const scopeLabels=[["value","数值 / 状态"],["unit","单位"],["material_id","材料归属"],["experiment_id","实验归属"],["time_window","时间窗口"]];
      let action=saved?.action||null,patch=structuredClone(saved?.patch||{}),onScopeChange=()=>{};
      const note=el("textarea",{class:"field-textarea",placeholder:"补充核查依据、原文位置、冲突内容或无法判断的原因…",value:saved?.note||"",style:"min-height:68px"});
      const scopeBox=el("div",{class:"review-scope"});
      for(const [key,label] of scopeLabels){
        const check=el("input",{type:"checkbox",checked:scopes.has(key),onchange:event=>{
          if(!event.target.checked&&scopes.size===1){event.target.checked=true;toast("至少保留一个明确的核查范围",true);return;}
          if(event.target.checked&&action==="not_reported"&&!["value","unit"].includes(key)){event.target.checked=false;toast("未报告结论只适用于数值和单位；其他归属问题请单独回查",true);return;}
          event.target.checked?scopes.add(key):scopes.delete(key);
          for(const field of Object.keys(patch))if(!scopes.has(field))delete patch[field];
          onScopeChange();persist();
        }});
        scopeBox.append(el("label",{},check,label));
      }
      const choiceBox=el("div",{class:"review-actions"}),editor=el("div");
      const choices=[["confirm","与原文一致"],["correct","需要修正"],["not_reported","已核查来源未报告"],["conflict","原文存在冲突"],["uncertain","暂不能判断"]];
      function persist(){
        if(!action)return;
        if(action==="not_reported"){
          patch={};
          for(const key of scopes)patch[key]={...structuredClone(o[key]),status:"not_reported",value:null,raw:null,normalized:null,normalization_reason:null,alternatives:[],note:note.value};
        }
        setEntry(o.id,{action,scope:[...scopes],patch:structuredClone(patch),note:note.value,evidence_ids:evidenceIds(o)});
      }
      note.addEventListener("input",persist);
      function drawEditor(){
        editor.replaceChildren();onScopeChange=()=>{};
        if(action==="correct"){
          const val=patch.value||o.value,unit=patch.unit||o.unit;
          const values=el("input",{class:"field-input",value:val.value??"",placeholder:factNames[val.status]||"原数值未给出","aria-label":"修正后的数值"});
          const units=el("input",{class:"field-input",value:unit.value??"",placeholder:factNames[unit.status]||"原单位未给出","aria-label":"修正后的单位"});
          const mat=select([["","未绑定材料"],...(d.record.materials||[]).map(x=>[x.id,fact(x.name)])],patch.material_id??o.material_id??"");
          const experiment=select((d.record.experiments||[]).map(x=>[x.id,(purposeNames[x.purpose]||x.purpose)+" · "+x.id]),patch.experiment_id||o.experiment_id);
          values.addEventListener("input",()=>{
            const text=values.value.trim();
            if(!text){delete patch.value;persist();return;}
            const numeric=/^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$/.test(text),value=numeric?Number(text):text;
            if(numeric&&!Number.isFinite(value)){toast("数值超出可表示范围",true);return;}
            patch.value={...structuredClone(o.value),value,status:isEstimated(o)?"estimated":"reported",raw:text,normalized:null,normalization_reason:null,alternatives:[],note:note.value||o.value.note};persist();
          });
          units.addEventListener("input",()=>{const value=units.value.trim();if(!value)delete patch.unit;else patch.unit={...structuredClone(o.unit),value,status:"reported",raw:value,normalized:null,normalization_reason:null,alternatives:[],note:note.value||o.unit.note};persist();});
          mat.addEventListener("change",()=>{patch.material_id=mat.value||null;persist();});
          experiment.addEventListener("change",()=>{patch.experiment_id=experiment.value;persist();});
          onScopeChange=()=>{values.disabled=!scopes.has("value");units.disabled=!scopes.has("unit");mat.disabled=!scopes.has("material_id");experiment.disabled=!scopes.has("experiment_id");};
          onScopeChange();
          editor.append(el("div",{class:"form-row"},field("修正数值",values),field("修正单位",units)),field("材料归属",mat),field("实验归属",experiment),el("div",{class:"review-note"},"只改写勾选且实际编辑的字段。材料或实验归属改变后需重新检查相关条件和配对；时间窗口及新增证据请提出定向回查。"));
        }
        if(action==="not_reported")editor.append(el("div",{class:"attachment-reason"},"仅在已核查相关正文与已取得 SI、仍未找到该信息时选择。看不清或 SI 未取得请选“暂不能判断”。"));
        if(action==="conflict")editor.append(el("div",{class:"attachment-reason"},"请写明双方原文位置和内容。此处登记冲突并使记录等待回查，保留双方证据后再更新事实，不任选一个数值。"));
      }
      function choose(value){
        if(value==="not_reported"&&[...scopes].some(x=>!["value","unit"].includes(x))){toast("未报告仅适用于数值和单位，请先调整本次核查范围",true);return;}
        if(action!==value)patch={};action=value;persist();drawChoices();drawEditor();
      }
      function drawChoices(){choiceBox.replaceChildren(...choices.map(([value,label])=>el("button",{type:"button",class:"review-choice"+(action===value?" selected":""),"aria-pressed":action===value,onclick:()=>choose(value)},el("span",{class:"choice-circle"}),label)));}
      const confirmNext=button("确认所选范围并下一项",()=>{choose("confirm");if(index<ids.length-1){index++;paint();}else toast("已记录草稿；请提交本次核查以登记人工结论");},"small soft","check");
      const discard=button("移除此项草稿",()=>{rs.entries.delete(o.id);schedule();paint();},"small text-button");
      drawChoices();drawEditor();
      form.append(scopeBox,choiceBox,editor,field("核查备注",note),el("div",{class:"review-inline-actions"},confirmNext,discard),button("请求模型回查此项",()=>recheckModal(d,o),"small text-button","spark"),el("div",{class:"review-note"},"草稿自动保存。提交后才记录人工结论；确认范围不会扩展到整篇论文。"));
      scroll.append(form);
    }
    function renderOmission(){const note=el("textarea",{class:"field-textarea",placeholder:"说明主要图表的位置、遗漏的最终材料/实验/结果，或你核查过的图表范围。",style:"min-height:130px"});scroll.append(quantitativeCoverageCard(d),quantitativeFollowupCard(d),el("h3",{class:"metric-name"},"从原文反向检查遗漏"),el("p",{class:"small-text muted"},"在左侧原文中查看主要性能图或表。发现模型没有提取的结果时，注明位置并交给模型补查。此操作不意味着已经核查整篇召回率。"),field("原文位置与查漏结论",note),button("提交定向补查任务",()=>perform(async()=>{if(!note.value.trim())throw new Error("请说明需要回查的原文位置与问题");await post("/api/recheck",{record_id:d.id,question:"反向查漏："+note.value});toast("补查任务已加入队列，结果会保留为新版本或回查建议");}),"primary","spark"));}
    paint();if(d.sources?.length&&!ids.length)source.load(d.sources[0].id);}
  function submitReview(d,rs){const entries=structuredClone([...rs.entries.values()]);if(!entries.length){toast("请先为至少一项结果选择核查结论",true);return;}for(const entry of entries){if(entry.action!=="confirm"&&!entry.note?.trim()){toast("修正、缺失或未解决的判断需要简短的原文依据，请补充核查备注",true);return;}if(entry.action==="correct"&&!Object.keys(entry.patch||{}).length){toast("修正结论需要至少一项实际修改；一致的内容请选择与原文一致",true);return;}}const reviewer=el("input",{class:"field-input",value:rs.reviewer});const counts={};for(const e of entries)counts[e.action]=(counts[e.action]||0)+1;const names={confirm:"确认一致",correct:"修正",not_reported:"未报告",conflict:"冲突",uncertain:"暂不能判断",omission:"遗漏"};const body=el("div",{},field("审核人",reviewer),el("div",{class:"note-panel",style:"margin-top:0"},el("strong",{},`本次提交 ${entries.length} 项局部核查`),el("p",{},Object.entries(counts).map(([k,v])=>`${names[k]||k} ${v} 项`).join(" · "))),el("p",{class:"small-text muted"},"仅登记各项勾选的范围。事实修正会生成新记录版本，原输出保留；提交成功不等于已同步Neo4j。"),el("ul",{class:"small-text",style:"max-height:200px;overflow:auto;padding-left:18px"},entries.map(e=>el("li",{style:"margin-bottom:8px"},`${fact(findObs(d,e.observation_id)?.metric)}：${names[e.action]}（${e.scope.join("、")}）`))));const dlg=openModal("提交本次人工核查","检查提交范围与版本，未核查内容不会变成人工已验证。",body);const run=button("提交局部核查",()=>perform(async()=>{const name=reviewer.value.trim();if(!name)throw new Error("请填写审核人");clearTimeout(rs.timer);if(rs.saving)throw new Error("草稿正在保存，请稍后再提交");rs.reviewer=name;localStorage.setItem("wt.reviewer",name);const result=await post(recordPath(d.id)+"/review",{reviewer:name,base_hash:rs.base_hash,entries});dlg.close();S.review.delete(d.id);S.details.clear();await loadBase();const next=result.record_id||result.id||d.id;toast("核查记录已提交。修正范围与历史版本均已保留。");route("review",next);await renderPage();},run),"primary","check");body.append(el("div",{class:"form-actions"},button("返回草稿",()=>dlg.close()),run));}
  function recheckModal(d,o){const question=el("textarea",{class:"field-textarea",placeholder:"请具体说明需要回查的问题，例如数值对应的材料、条件、图表位置或正文/SI冲突。"});const body=el("div",{},o?el("div",{class:"material-chip"},fact(o.metric)+" · "+observationValueText(o)+" "+fact(o.unit)):null,field("回查问题",question),el("p",{class:"small-text muted"},`使用 ${currentModelLabel()} 回查，结果保留为独立的模型审查版本。`));const dlg=openModal("请求模型回到原文检查","回查作为独立后台任务执行。",body);const run=button("开始回查",()=>perform(async()=>{if(!question.value.trim())throw new Error("请填写具体的回查问题");await post("/api/recheck",{record_id:d.id,question:question.value,observation_id:o?.id});dlg.close();toast("已加入定向回查任务");await loadBase();},run),"primary","spark");body.append(el("div",{class:"form-actions"},button("取消",()=>dlg.close()),run));}
  // Markdown is rendered from a small allowlist of DOM nodes, never HTML.
  function appendInlineMarkdown(target,value) {
    const text=String(value??""),tokens=/(`[^`\n]+`|\*\*[^\n]+?\*\*)/g;
    let cursor=0,match;
    while((match=tokens.exec(text))!==null){
      if(match.index>cursor)target.append(document.createTextNode(text.slice(cursor,match.index)));
      const token=match[0];
      if(token[0]==="`")target.append(el("code",{},token.slice(1,-1)));
      else {const strong=el("strong");appendInlineMarkdown(strong,token.slice(2,-2));target.append(strong);}
      cursor=tokens.lastIndex;
    }
    if(cursor<text.length)target.append(document.createTextNode(text.slice(cursor)));
    return target;
  }
  function markdownTableCells(line) {
    let text=line.trim();if(text.startsWith("|"))text=text.slice(1);if(text.endsWith("|")&&!text.endsWith("\\|"))text=text.slice(0,-1);
    const cells=[];let part="",escaped=false,inCode=false;
    for(let index=0;index<text.length;index++){
      const char=text[index];
      if(escaped){part+=char;escaped=false;continue;}
      if(char==="\\"&&["|","\\"].includes(text[index+1])){escaped=true;continue;}
      if(char==="`")inCode=!inCode;
      if(char==="|"&&!inCode){cells.push(part.trim());part="";}else part+=char;
    }
    if(escaped)part+="\\";cells.push(part.trim());return cells;
  }
  function renderMarkdown(value) {
    const root=el("div",{class:"message-body markdown-body"});
    const lines=String(value??"").replace(/\r\n?/g,"\n").split("\n");
    const listMatch=line=>line.match(/^\s*(?:([-+*])|(\d+)[.)])\s+(.+)$/);
    const tableAt=index=>index+1<lines.length&&lines[index].includes("|")&&markdownTableCells(lines[index+1]).every(cell=>/^:?-{3,}:?$/.test(cell))&&markdownTableCells(lines[index]).length===markdownTableCells(lines[index+1]).length;
    const blockAt=index=>/^\s*```/.test(lines[index])||/^#{1,6}\s+/.test(lines[index])||!!listMatch(lines[index])||tableAt(index);
    let index=0;
    while(index<lines.length){
      if(!lines[index].trim()){index++;continue;}
      if(/^\s*```/.test(lines[index])){
        const code=[];index++;
        while(index<lines.length&&!/^\s*```/.test(lines[index]))code.push(lines[index++]);
        if(index<lines.length)index++;
        root.append(el("pre",{},el("code",{},code.join("\n"))));continue;
      }
      const heading=lines[index].match(/^(#{1,6})\s+(.+?)\s*#*$/);
      if(heading){root.append(appendInlineMarkdown(el(heading[1].length<=2?"h3":"h4"),heading[2]));index++;continue;}
      if(tableAt(index)){
        const headers=markdownTableCells(lines[index]),dividers=markdownTableCells(lines[index+1]);
        const align=dividers.map(cell=>cell.startsWith(":")&&cell.endsWith(":")?"center":cell.endsWith(":")?"right":"left");
        const head=el("thead",{},el("tr",{},headers.map((cell,col)=>appendInlineMarkdown(el("th",{scope:"col",style:"text-align:"+align[col]}),cell))));
        const body=el("tbody");index+=2;
        while(index<lines.length&&lines[index].trim()&&lines[index].includes("|")){
          const row=markdownTableCells(lines[index]);if(row.length!==headers.length)break;
          body.append(el("tr",{},row.map((cell,col)=>appendInlineMarkdown(el("td",{style:"text-align:"+align[col]}),cell))));index++;
        }
        root.append(el("div",{class:"markdown-table-wrap",tabindex:0,"aria-label":"回答中的对比表，可横向滚动"},el("table",{},head,body)));continue;
      }
      const match=listMatch(lines[index]);
      if(match){
        const ordered=!!match[2],list=el(ordered?"ol":"ul",ordered?{start:Number(match[2])}:{});
        while(index<lines.length){const item=listMatch(lines[index]);if(!item||!!item[2]!==ordered)break;list.append(appendInlineMarkdown(el("li"),item[3]));index++;}
        root.append(list);continue;
      }
      const paragraph=[lines[index++]];
      while(index<lines.length&&lines[index].trim()&&!blockAt(index))paragraph.push(lines[index++]);
      root.append(appendInlineMarkdown(el("p"),paragraph.join("\n")));
    }
    return root;
  }
  function citationLabel(citation,index) {
    const label=String(citation.label||citation.quote?.slice(0,80)||"查看原文证据").replace(/^(?:\s*\[\d+\]\s*)+/,"");
    return `[${index+1}] ${label||"查看原文证据"}`;
  }
  function historicalAnswerScope(answer) {
    if(Array.isArray(answer.record_ids)){
      const papers=new Set(answer.record_ids.map(id=>recordSummary(id).paper_id||id));
      return `本回答来源：${papers.size} 篇文献 · ${answer.scope==="published"?"生成时的图谱有效版本":"生成时所选记录"}`;
    }
    const cited=new Set((answer.citations||[]).filter(x=>x.record_id).map(x=>recordSummary(x.record_id).paper_id||x.record_id));
    return cited.size?`本回答引用 ${cited.size} 篇文献 · 完整历史来源范围未登记`:"本回答的历史来源范围未登记";
  }
  function qa(){const m=$("#main");m.replaceChildren(pageHeader("文献问答","围绕所选记录回答，每个科研结论都应能回到实验与原文。",[],"ASK YOUR EVIDENCE"));const sources=latestRecords();const side=el("aside",{class:"qa-side"},el("h3",{},"选择问答来源"));let scope="selected";const mode=select([["selected","选中的记录"],["published","图谱当前有效记录"]],scope,{"aria-label":"问答来源范围"});const sourceList=el("div",{class:"qa-source-list"});const messages=el("div",{class:"qa-messages"});const label=el("span");const busy=el("span",{class:"tiny muted"});const question=el("textarea",{placeholder:"例如：这几篇材料分别用什么氧化剂处理什么污染物？","aria-label":"输入文献问题"});const send=button("发送问题",()=>perform(async()=>{const text=question.value.trim();if(!text)throw new Error("请先输入问题");if(scope==="selected"&&!S.qaSelected.size)throw new Error("请先选择至少一篇已有结果的文献");const result=await post("/api/ask",{question:text,record_ids:[...S.qaSelected],scope});question.value="";messages.append(el("div",{class:"message user"},el("div",{class:"message-label"},"你 · 已提交后台任务"),el("div",{class:"message-body"},text)));busy.textContent="模型正在结合证据回答…";toast("问题已提交，回答将自动显示");await loadBase();S.lastAsk=result.job_id;if(S.qaRefresh)S.qaRefresh();},send),"primary","arrow");question.addEventListener("keydown",e=>{if(e.key==="Enter"&&(e.ctrlKey||e.metaKey)){e.preventDefault();send.click();}});const workspace=el("section",{class:"qa-workspace"},el("div",{class:"qa-scope"},label,busy),messages,el("div",{class:"qa-composer"},el("div",{class:"qa-input-wrap"},question,send),el("div",{class:"tiny"},"Ctrl + Enter 发送 · 问答只读来源，纠错通过回查与版本流程进行")));side.append(el("div",{style:"padding:12px 14px 0"},mode),sourceList);m.append(el("div",{class:"qa-layout"},side,workspace));function paintSources(){sourceList.replaceChildren(...sources.map(r=>el("label",{class:"qa-source-row"},el("input",{type:"checkbox",checked:S.qaSelected.has(r.id),disabled:scope==="published",onchange:e=>{e.target.checked?S.qaSelected.add(r.id):S.qaSelected.delete(r.id);updateScope();}}),el("span",{},r.title||r.paper_id,el("small",{},(statusNames[r.publish_status]||"未入库")+" · "+(r.doi||"未提供DOI"))))));if(!sources.length)sourceList.append(el("p",{class:"tiny muted"},"目前没有可选择的抽取结果。"));updateScope();}function updateScope(){label.textContent=scope==="published"?"下一次提问来源：图谱当前有效记录":"下一次提问来源：已选择 "+S.qaSelected.size+" 篇文献结果";}mode.addEventListener("change",()=>{scope=mode.value;paintSources();});function paintAnswers(){messages.replaceChildren();if(!S.answers.length){const prompts=["这些最终材料分别处理了哪些污染物？","蒸发率和催化效果是否来自同一条件组？","哪些稳定性结果有原文支持？","比较这些体系使用的催化剂与氧化剂。"];messages.append(el("div",{class:"qa-intro"},el("div",{class:"qa-symbol"},icon("spark")),el("h2",{},"让每个回答有据可查"),el("p",{},"选择文献范围后开始提问。材料、实验条件与原文证据会一起进入讨论。"),el("div",{class:"qa-suggestions"},prompts.map(t=>el("button",{type:"button",class:"qa-suggestion",onclick:()=>{question.value=t;question.focus();}},t)))));return;}for(const a of [...S.answers].reverse()){messages.append(el("div",{class:"message user"},el("div",{class:"message-label"},"你的问题 · "+shortDate(a.created_at)),el("div",{class:"message-body"},a.question)));const text=typeof a.answer==="string"?a.answer:a.answer?.text||a.answer?.answer||JSON.stringify(a.answer||{});const answer=el("div",{class:"message assistant"},el("div",{class:"message-label"},icon("spark"),modelLabel(a.model_metadata?.server_selected_model || a.model, a.model_metadata?.server_selected_effort || a.effort)),el("div",{class:"answer-source-scope"},historicalAnswerScope(a)),renderMarkdown(text));if(a.insufficient_evidence)answer.append(el("div",{class:"inline-notice"},"现有证据不足以完成全部判断，请结合下方范围与限制理解回答。"));if(a.citations?.length)answer.append(el("div",{class:"answer-citations"},a.citations.map((c,i)=>el("button",{type:"button",class:"citation-chip",onclick:()=>{if(c.record_id&&c.evidence_id)evidenceModal(c.record_id,c.evidence_id);else if(c.record_id)route("records",c.record_id);}},citationLabel(c,i)))));if(a.limitations?.length)answer.append(el("details",{class:"details-section"},el("summary",{},"回答范围与限制"),el("ul",{},a.limitations.map(x=>el("li",{},x)))));messages.append(answer);}messages.scrollTop=messages.scrollHeight;}paintSources();paintAnswers();S.qaRefresh=()=>{if(S.page!=="qa")return;const active=S.jobs.some(j=>["ask","qa"].includes(j.kind)&&["pending","queued","running"].includes(j.status));const failed=S.jobs.find(j=>j.id===S.lastAsk&&["failed","interrupted","cancelled"].includes(j.status));busy.textContent=active?"模型正在结合证据回答…":failed?"回答未完成，请到任务页查看并重试":"";const key=S.answers.map(x=>x.id).join("|");if(messages.dataset.answerKey!==key){messages.dataset.answerKey=key;paintAnswers();}};messages.dataset.answerKey=S.answers.map(x=>x.id).join("|");}
  function settingsModal(){const st=S.status;const body=el("div",{},el("dl",{class:"info-list"},el("dt",{},"运行方式"),el("dd",{},"本机服务 · 浏览器工作台"),el("dt",{},"模型"),el("dd",{},currentModelLabel()),el("dt",{},"抽取规则"),el("dd",{},st.rules_version||"尚未提供"),el("dt",{},"附件识别规则"),el("dd",{},st.attachment_classifier_version||"后台尚未提供版本信息"),el("dt",{},"文献核实清单"),el("dd",{},({loaded:"已加载",not_configured:"未配置",unavailable:"不可用，请检查配置"})[st.collection_policy_status]||"后台尚未提供清单状态"),el("dt",{},"来源目录"),el("dd",{class:"mono"},(st.source_roots||[]).join("\n")),el("dt",{},"版本"),el("dd",{},st.version||"—")),el("div",{class:"note-panel"},el("strong",{},"建议的使用顺序"),el("p",{},"扫描文献目录 → 确认少量不确定附件 → 选择文献抽取 → 查看结果与证据 → 少量人工抽查 → 按策略同步图谱。")),el("p",{class:"small-text muted",style:"margin-top:18px"},"原件、模型输出、人工核查和图谱版本分别留存。未报告与未核查保持明确状态，不能把机器校验通过当作科学准确率。"));body.prepend(runtimeVersionDetails());openModal("运行环境与使用说明","凭据由本地后台管理，不在普通页面展示。",body);}
  async function renderPage(){
    const {page,id,params}=routeState();S.page=page;
    const choice=(key,allowed,fallback="all")=>allowed.includes(params.get(key))?params.get(key):fallback;
    if(page==="library"){
      S.query=params.get("q")||"";S.filter=choice("attachments",["all","ready","needs_confirmation"]);
      S.extractionFilter=choice("extraction",["all","not_started","active","completed","unfinished","has_candidate"]);
    }else if(page==="records"){
      S.recordQuery=params.get("q")||"";S.recordFilter=choice("filter",["all","completed","unfinished","published","reviewed"]);
      S.showHistory=S.recordFilter==="all"&&params.get("history")==="1";
    }else if(page==="jobs"){
      S.jobQuery=params.get("q")||"";S.jobFilter=choice("filter",["all","active","completed","recovered","unfinished","history_failed","cancelled"]);
    }else if(page==="review")S.reviewFilter=choice("filter",["all","reviewed"]);
    renderNav();
    if((page==="records"||page==="review")&&id){await recordView(id,page==="review");return;}
    ({overview,library,jobs,review:reviews,records,qa}[page])();
  }
  async function poll(){
    if(S.pollBusy||!S.initialized||document.hidden)return;S.pollBusy=true;
    try{
      const jobKey=rows=>rows.map(j=>[j.id,j.status,j.updated_at,j.display_status,j.current_result_id].join(":" )).join("|");
      const before=jobKey(S.jobs),[data,runtime]=await Promise.all([api("/api/jobs"),api("/api/runtime").catch(error=>{if(error.status===404)return null;throw error;})]);
      S.status.runtime_code=runtime;renderRuntimeStatus();
      const changed=before!==jobKey(data.jobs||[])||data.activity_revision!==undefined&&data.activity_revision!==S.activityRevision;
      const retryCompatibility=!S.workspaceReady&&Date.now()-S.lastLegacyRefresh>30000;
      if(changed||retryCompatibility){
        const [status,workspace,samples,answers]=await Promise.all([api("/api/status"),loadWorkspace(),api("/api/samples"),api("/api/answers")]);
        S.status=status;renderRuntimeStatus();applyWorkspace(workspace);S.samples=samples.samples||[];S.answers=answers.answers||[];
        S.samples.sort((a,b)=>String(b.created_at).localeCompare(String(a.created_at)));S.details.clear();renderNav();
        if(S.page==="qa"&&S.qaRefresh)S.qaRefresh();
        else if(!routeState().id&&["jobs","overview","library","records","review"].includes(S.page))await renderPage();
      }else{S.jobs=data.jobs||[];S.status.paused=data.paused;S.status.sync_state=data.sync_state;}
      updateQueueControl();
      $("#connection-dot").className="online-dot ready";$("#connection-label").textContent="本地服务已连接";
    }catch(e){$("#connection-dot").className="online-dot error";$("#connection-label").textContent="连接中断 · 等待恢复";}
    finally{S.pollBusy=false;}
  }
  $("#settings-button").addEventListener("click",settingsModal);$("#refresh-button").addEventListener("click",()=>refresh(true));window.addEventListener("hashchange",()=>perform(renderPage));for(const d of document.querySelectorAll("dialog"))d.addEventListener("click",e=>{if(e.target===d){const r=d.getBoundingClientRect();if(e.clientX<r.left||e.clientX>r.right||e.clientY<r.top||e.clientY>r.bottom)d.close();}});window.addEventListener("beforeunload",e=>{if([...S.review.values()].some(r=>r.dirty||r.saving)){e.preventDefault();e.returnValue="";}});
  (async()=>{try{await loadBase();await renderPage();}catch(e){$("#connection-dot").className="online-dot error";$("#connection-label").textContent="本地服务不可用";$("#main").replaceChildren(errorPanel(e,()=>location.reload()));}setInterval(poll,4000);})();
})();
