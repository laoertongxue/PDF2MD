import { createServer } from "node:http";

const now = 1_720_000_000;
const json = (res, status, body) => {
  res.writeHead(status, { "content-type": "application/json", "access-control-allow-origin": "*", "access-control-allow-headers": "content-type", "access-control-allow-methods": "GET,POST,PUT,PATCH,DELETE,OPTIONS" });
  res.end(JSON.stringify(body));
};

export function startFixtureServer(port) {
  const state = seed();
  const requests = [];
  const server = createServer(async (req, res) => {
    if (req.method === "OPTIONS") return json(res, 204, null);
    const url = new URL(req.url, `http://${req.headers.host}`);
    const body = await readBody(req);
    requests.push({ method: req.method, path: url.pathname, body });
    if (url.pathname === "/__fixture/requests") return json(res, 200, requests);
    if (url.pathname === "/__fixture/reset" && req.method === "POST") { Object.assign(state, seed()); requests.length = 0; return json(res, 200, { ok: true }); }
    try { return route(state, req.method, url.pathname, body, res); }
    catch (error) { return json(res, 500, { detail: String(error) }); }
  });
  return new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(port, "127.0.0.1", () => resolve({ server, state, requests }));
  });
}

function seed() {
  const course = { id: "course-1", title: "企业战略与组织协同", description: "MBA", root_dir: "/fixture/mba", created_at: now, updated_at: now };
  const sources = [source("source-1", "战略管理"), source("source-2", "组织行为学")];
  const chapters = {
    "source-1": [chapter("chapter-1", "source-1", 0, "第一章 管理导论"), chapter("chapter-2", "source-1", 1, "第二章 竞争战略")],
    "source-2": [chapter("chapter-3", "source-2", 0, "第一章 管理导论"), chapter("chapter-4", "source-2", 1, "第二章 组织动机")],
  };
  const drafts = Object.fromEntries(sources.map((s) => [s.id, { chapters: chapters[s.id].map((c, i) => ({ ...c, start: i * 10, end: (i + 1) * 10 })), fingerprint: `fp-${s.id}-1` }]));
  const topic = { id: "topic-1", course_id: course.id, seq: 0, title: "战略选择", description: "竞争定位到组织执行", generation_reason: "fixture", status: "FAILED", confirmed: true, stale_reason: "", chapter_ids: ["chapter-1", "chapter-3"], blocking_chapter_ids: [], sync_status: "SYNCED", sync_error: "" };
  return { courses: [course], sources, chapters, drafts, topic, blocks: noteBlocks(), topicBlocks: fusionBlocks(), cards: cards(), topicRuns: [{ id: "tr-1", topic_id: topic.id, round_key: "review", status: "FAILED", input_fingerprint: "fp", output: "", error: "模型服务暂时不可用", started_at: now, finished_at: now }], chapterRuns: chapterRuns() };
}
function source(id, title) { return { id, course_id: "course-1", kind: "main", file_path: `/fixture/${title}.pdf`, title, markdown_path: null, status: "READY", created_at: now, updated_at: now }; }
function chapter(id, source_id, seq, title) { return { id, source_id, course_id: "course-1", seq, title, status: "DRAFT", created_at: now, updated_at: now }; }
function noteBlocks() { return [
  ["summary", "本章概要", "## 战略概要\n定位与执行 [《战略管理》·第 1 章]"], ["concepts", "核心概念", "成本领先与差异化"], ["plain_explain", "通俗解释", "明确选择并持续执行。"], ["application", "应用场景", "用于业务组合。"], ["reflection", "复盘反思", "识别能力边界。"], ["knowledge_mermaid", "知识结构图", "flowchart LR\nA[战略]-->B[组织]"], ["application_mermaid", "应用流程图", "flowchart LR\nC[分析]-->D[执行]"],
].map(([kind,title,body], seq) => ({ id:`nb-${seq}`, chapter_id:"chapter-1", kind,title,body,seq,updated_at:now })); }
function fusionBlocks() { const kinds=["overview","linked_sources","core_concepts","viewpoint_comparison","consensus_disagreements","complementary_views","plain_explanation","textbook_cases","real_world_problem_solving","integrated_framework","application_methods","further_thinking","knowledge_mermaid","application_mermaid"]; return kinds.map((kind,i)=>({id:`tb-${i}`,topic_id:"topic-1",kind,content:kind.endsWith("mermaid")?`flowchart LR\nX${i}[主题]-->Y${i}[行动]`:`${kind} [《战略管理》·第 1 章] [《组织行为学》·第 1 章]`,updated_at:now})); }
function cards() { return [{id:"card-1",origin_type:"chapter",origin_id:"chapter-1",origin_title:"第一章 管理导论",card_type:"观点",title:"竞争优势不是单点能力",content:"战略与组织需要协同。",source_refs:[],tags:["战略"],status:"ACTIVE",favorite:false,updated_at:now},{id:"card-2",origin_type:"topic",origin_id:"topic-1",origin_title:"战略选择",card_type:"方法",title:"组织结构服务战略",content:"结构跟随战略。",source_refs:[],tags:["组织"],status:"ACTIVE",favorite:false,updated_at:now}]; }
function chapterRuns(){return [{id:"cr-1",chapter_id:"chapter-1",round_key:"structure",executor:"deepseek",status:"COMPLETED",output:"ok",error:"",stale:false,created_at:now,updated_at:now},{id:"cr-2",chapter_id:"chapter-1",round_key:"review",executor:"codex",status:"FAILED",output:"",error:"引用不足",stale:false,created_at:now+1,updated_at:now+1}];}

function route(s, method, path, body, res) {
  let m;
  if (path === "/api/workbench/courses" && method === "GET") return json(res,200,s.courses);
  if (path === "/api/workbench/courses" && method === "POST") { const item={id:`course-${s.courses.length+1}`,...body,created_at:now,updated_at:now}; s.courses.push(item); return json(res,200,item); }
  if ((m=path.match(/^\/api\/workbench\/courses\/([^/]+)\/sources$/)) && method === "GET") return json(res,200,s.sources.filter(x=>x.course_id===m[1]));
  if ((m=path.match(/^\/api\/workbench\/sources\/([^/]+)\/chapters$/))) return json(res,200,s.chapters[m[1]]??[]);
  if ((m=path.match(/^\/api\/workbench\/sources\/([^/]+)\/chapter-drafts$/)) && method === "GET") return json(res,200,s.drafts[m[1]]);
  if ((m=path.match(/^\/api\/workbench\/sources\/([^/]+)\/chapter-drafts$/)) && method === "PUT") { const old=s.drafts[m[1]]; old.chapters=body.chapters.map((c,i)=>({id:c.id?.startsWith("local:")?`chapter-split-${i}`:c.id??`chapter-new-${i}`,source_id:m[1],course_id:"course-1",...c,seq:i,status:"DRAFT"})); old.fingerprint=`fp-${m[1]}-saved`; return json(res,200,old); }
  if ((m=path.match(/^\/api\/workbench\/sources\/([^/]+)\/chapter-drafts\/confirm$/)) && method === "POST") { const old=s.drafts[m[1]]; old.chapters=old.chapters.map(c=>({...c,status:"CONFIRMED"})); old.fingerprint=`fp-${m[1]}-locked`; s.chapters[m[1]]=old.chapters; return json(res,200,old); }
  if ((m=path.match(/^\/api\/workbench\/chapters\/([^/]+)\/note-blocks$/))) return json(res,200,m[1]==="chapter-1"?s.blocks:[]);
  if ((m=path.match(/^\/api\/workbench\/chapters\/([^/]+)\/runs$/))) return json(res,200,m[1]==="chapter-1"?s.chapterRuns:[]);
  if ((m=path.match(/^\/api\/workbench\/chapters\/([^/]+)\/note-blocks\/([^/]+)$/)) && method === "PATCH") { const item=s.blocks.find(x=>x.kind===m[2]); item.body=body.body; item.updated_at++; return json(res,200,item); }
  if ((m=path.match(/^\/api\/workbench\/chapters\/([^/]+)\/run-hybrid$/)) && method === "POST") { s.chapterRuns=s.chapterRuns.map(r=>r.status==="FAILED"?{...r,status:"COMPLETED",error:"",output:"rerun ok",updated_at:r.updated_at+1}:r); return json(res,200,{...s.chapters["source-1"][0],status:"COMPLETED"}); }
  if ((m=path.match(/^\/api\/workbench\/courses\/([^/]+)\/topics$/)) && method === "GET") return json(res,200,[s.topic]);
  if ((m=path.match(/^\/api\/workbench\/topics\/([^/]+)$/)) && method === "PATCH") { Object.assign(s.topic,body); return json(res,200,s.topic); }
  if ((m=path.match(/^\/api\/workbench\/topics\/([^/]+)\/chapters$/)) && method === "PUT") { s.topic.chapter_ids=body.chapter_ids; return json(res,200,s.topic); }
  if ((m=path.match(/^\/api\/workbench\/topics\/([^/]+)\/note-blocks$/))) return json(res,200,s.topicBlocks);
  if ((m=path.match(/^\/api\/workbench\/topics\/([^/]+)\/cards$/))) return json(res,200,s.cards.filter(c=>c.origin_type==="topic"));
  if ((m=path.match(/^\/api\/workbench\/topics\/([^/]+)\/runs$/))) return json(res,200,s.topicRuns);
  if ((m=path.match(/^\/api\/workbench\/topics\/([^/]+)\/note-blocks\/([^/]+)$/)) && method === "PATCH") { const item=s.topicBlocks.find(x=>x.kind===m[2]); item.content=body.content; item.updated_at++; return json(res,200,item); }
  if ((m=path.match(/^\/api\/workbench\/topics\/([^/]+)\/recover$/)) && method === "POST") { s.topic.status="READY"; s.topicRuns=s.topicRuns.map(r=>({...r,status:"FAILED",error:"已结束过期任务"})); return json(res,200,s.topic); }
  if ((m=path.match(/^\/api\/workbench\/topics\/([^/]+)\/run-hybrid$/)) && method === "POST") { s.topic.status="COMPLETED"; s.topicRuns.push({id:"tr-2",topic_id:s.topic.id,round_key:"review",status:"COMPLETED",input_fingerprint:"fp2",output:"ok",error:"",started_at:now+2,finished_at:now+3}); return json(res,200,s.topic); }
  if ((m=path.match(/^\/api\/workbench\/courses\/([^/]+)\/cards$/))) return json(res,200,s.cards);
  if ((m=path.match(/^\/api\/workbench\/cards\/([^/]+)$/)) && method === "PATCH") { const c=s.cards.find(x=>x.id===m[1]); Object.assign(c,body,{updated_at:c.updated_at+1}); return json(res,200,c); }
  if ((m=path.match(/^\/api\/workbench\/cards\/([^/]+)\/favorite$/)) && method === "PATCH") { const c=s.cards.find(x=>x.id===m[1]); c.favorite=body.favorite;c.updated_at++;return json(res,200,c); }
  return json(res,404,{detail:`No fixture for ${method} ${path}`});
}
async function readBody(req){const chunks=[];for await(const c of req)chunks.push(c);if(!chunks.length)return undefined;try{return JSON.parse(Buffer.concat(chunks).toString("utf8"));}catch{return undefined;}}
