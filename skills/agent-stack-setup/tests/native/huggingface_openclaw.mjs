// Offline acceptance against an installed, pinned @openclaw/ai distribution.
// Usage: node this-file.mjs /path/to/node_modules/@openclaw/ai/dist
import assert from 'node:assert/strict';
import {readFileSync, readdirSync} from 'node:fs';
import {pathToFileURL} from 'node:url';
import path from 'node:path';
const dist=path.resolve(process.argv[2]);
assert.equal(JSON.parse(readFileSync(path.join(dist,'../package.json'))).version,'2026.10.4');
const catalog=JSON.parse(readFileSync(new URL('../../../../modules/common/runtime/huggingface-models.json',import.meta.url)));
// These are pinned distribution internals; resolve exported names rather than
// assuming a minifier alias or a particular hashed filename.
async function bundledFunction(directory,prefix,name){
 for(const file of readdirSync(directory).filter(x=>x.startsWith(prefix)&&x.endsWith('.mjs'))){
  const source=readFileSync(path.join(directory,file),'utf8');
  const exports=source.match(/export\s*\{([^}]+)\}/)?.[1]??'';
  const alias=exports.match(new RegExp('\\b'+name+' as ([A-Za-z0-9_$]+)\\b'))?.[1];
  if(alias){
   const module=await import(pathToFileURL(path.join(directory,file)));
   assert.equal(typeof module[alias],'function',name);
   return module[alias];
  }
 }
 throw new Error('Pinned native function unavailable: '+name);
}
const {configureAiTransportHost}=await import(pathToFileURL(path.join(dist,'index.mjs')));
const request=await bundledFunction(dist,'openai-completions-transport-','streamOpenAICompletionsRequest');
const openclawDist=path.resolve(dist,'../../../openclaw/dist');
const resolveThinking=await bundledFunction(openclawDist,'thinking-','resolveThinkingSelectionForModel');
const applyExtraParams=await bundledFunction(openclawDist,'extra-params-','applyExtraParamsToAgent');
const buildCatalog=await bundledFunction(openclawDist,'model-selection-shared-','buildConfiguredModelCatalog');
const configuredThinking=await bundledFunction(openclawDist,'model-thinking-default-','resolveConfiguredThinkingDefaultCore');
const stream=(model,context,options)=>request(model,context,options,'managed');
globalThis.fetch=()=>{throw new Error('External fetch prohibited by offline acceptance');};
let captured=[],phase=0,count=1;
const reasoning=' synthetic reasoning fixture\nwith whitespace Ω ';
configureAiTransportHost({buildModelFetch:()=>async(input,init)=>{
 const url=typeof input==='string'?input:input instanceof URL?input.href:input.url;
 assert.equal(url,'https://router.huggingface.co/v1/chat/completions');
 const body=init?.body??await input.text();
 captured.push(JSON.parse(typeof body==='string'?body:Buffer.from(body).toString()));
 const toolTurn=phase++===0;
 const tools=Array.from({length:count},(_,i)=>({index:i,id:`call_fixture_${i}`,type:'function',function:{name:'sum_fixture',arguments:JSON.stringify({a:2+i,b:3})}}));
 const chunks=[{choices:[{index:0,delta:{role:'assistant',reasoning_content:reasoning},finish_reason:null}]},
  {choices:[{index:0,delta:toolTurn?{tool_calls:tools}:{content:'Fixture complete.'},finish_reason:null}]},
  {choices:[{index:0,delta:{},finish_reason:toolTurn?'tool_calls':'stop'}]}];
 return new Response(chunks.map(c=>'data: '+JSON.stringify(c)+'\n\n').join('')+'data: [DONE]\n\n',{headers:{'content-type':'text/event-stream'}});
}});
const results=[];
for(const metadata of catalog)for(count of [1,2]){
 const model={...metadata,provider:'huggingface',api:'openai-completions',baseUrl:'https://router.huggingface.co/v1'};
 for(const effort of [...new Set([...model.compat.supportedReasoningEfforts,...Object.keys(model.thinkingLevelMap),'off'])]){
  captured=[];phase=0;
  const cfg={agents:{defaults:{models:{['huggingface/'+model.id]:{params:{parallel_tool_calls:false,thinking:'high'}}}}},models:{providers:{huggingface:{baseUrl:model.baseUrl,api:model.api,models:catalog}}}};
  assert.equal(configuredThinking({cfg,provider:'huggingface',model:model.id}),'high');
  const projected=buildCatalog({cfg});
  assert.deepEqual(projected.find(m=>m.id===model.id).thinkingLevelMap,model.thinkingLevelMap);
  const selected=resolveThinking({provider:'huggingface',model:model.id,catalog:projected,level:effort,providerPolicySource:{providers:[]}});
  assert.notEqual(selected.level,'off');
  if(effort==='off')assert.equal(selected.supported,false);
  const user={role:'user',content:'Exercise the synthetic fixture.',timestamp:0};
  const tools=[{name:'sum_fixture',description:'Synthetic offline tool',parameters:{type:'object',properties:{a:{type:'number'},b:{type:'number'}},required:['a','b']}}];
  const options={apiKey:'hf_OfflineFixtureOnly',reasoningEffort:selected.level,maxTokens:8192};
  const agent={streamFn:stream};
  applyExtraParams(agent,cfg,'huggingface',model.id,undefined,selected.level,undefined,undefined,model,undefined,undefined,{preparedExtraParams:{parallel_tool_calls:false,thinking:'high'}});
  const managedStream=agent.streamFn;
  const first=await managedStream(model,{messages:[user],tools},options).result();
  assert.equal(first.stopReason,'toolUse',JSON.stringify(first));
  assert.ok(first.content.find(b=>b.type==='thinking'),JSON.stringify({model:model.id,effort,first,payload:captured[0]}));
  assert.equal(first.content.find(b=>b.type==='thinking').thinking,reasoning);
  assert.equal(first.content.find(b=>b.type==='thinking').thinkingSignature,'reasoning_content');
  assert.equal(first.content.filter(b=>b.type==='toolCall').length,count);
  const toolResults=Array.from({length:count},(_,i)=>({role:'toolResult',toolCallId:`call_fixture_${i}`,toolName:'sum_fixture',content:[{type:'text',text:String(5+i)}],isError:false,timestamp:1}));
  const history=[user,first,...toolResults];
  const final=await managedStream(model,{messages:history,tools},options).result();
  assert.equal(final.stopReason,'stop',JSON.stringify(final));
  const followup={role:'user',content:'Continue this synthetic session.',timestamp:2};
  const continued=await managedStream(model,{messages:[...history,final,followup],tools},options).result();
  assert.equal(continued.stopReason,'stop',JSON.stringify(continued));
  for(const payload of captured){
   assert.equal(payload.model,model.id);assert.equal(payload.parallel_tool_calls,false);
   assert.deepEqual(Object.keys(payload).sort(),['model','messages','stream','stream_options','tools','max_tokens','reasoning_effort','parallel_tool_calls'].sort());
   assert.equal(payload.max_tokens,8192);assert.equal(payload.max_completion_tokens,undefined);
   assert.equal(payload.reasoning_effort,selected.level);
  }
  for(const payload of captured.slice(1)){
   const replay=payload.messages.find(m=>m.role==='assistant');
   assert.equal(replay.reasoning_content,reasoning);assert.equal(replay.tool_calls.length,count);
   for(let i=0;i<count;i++){
    assert.equal(replay.tool_calls[i].id,`call_fixture_${i}`);
    assert.deepEqual(JSON.parse(replay.tool_calls[i].function.arguments),{a:2+i,b:3});
    assert.ok(payload.messages.some(m=>m.role==='tool'&&m.tool_call_id===`call_fixture_${i}`));
   }
  }
  assert.equal(captured[2].messages.filter(m=>m.role==='assistant')[1].reasoning_content,reasoning);
  results.push({id:model.id,requested_effort:effort,execution_effort:selected.level,wire_effort:captured[0].reasoning_effort,tool_calls:count,request_keys:Object.keys(captured[0]),status:'passed'});
 }
}
console.log(JSON.stringify({boundary:'OpenClaw managed SSE parser, serializer and same-session follow-up; mocked in-memory fetch; no model semantics or live API claim',real_inference_calls:0,results},null,2));
