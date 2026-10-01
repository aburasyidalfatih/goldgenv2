const {test} = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
function app(fetch) {
    const context = vm.createContext({fetch, console, setTimeout, clearTimeout, setInterval, clearInterval});
    vm.runInContext(fs.readFileSync('static/js/app.js','utf8'),context);
    const state = context.autoPosterApp();
    state.showToast = () => {};
    state.$nextTick = fn => fn();
    return state;
}
test('older page response cannot overwrite new page data', async () => {
    const pending = [];
    const s = app(() => new Promise(resolve => pending.push(resolve)));
    s.activePageId = 1;
    const first = s.fetchAnalytics();
    s.activePageId = 2;
    const second = s.fetchAnalytics();
    pending[1]({ok:true,json:async()=>({page_id:2,total_posts:8})}); await second;
    pending[0]({ok:true,json:async()=>({page_id:1,total_posts:2})}); await first;
    assert.equal(s.analyticsSummary.page_id,2);
});
test('late request for same page cannot replace newer results', async () => {
    const pending=[]; const s=app(()=>new Promise(r=>pending.push(r)));
    const first=s.fetchTopics(), second=s.fetchTopics();
    pending[1]({ok:true,json:async()=>[{id:2}]}); await second;
    pending[0]({ok:true,json:async()=>[{id:1}]}); await first;
    assert.equal(s.topics[0].id,2);
});
test('save draft does not clear edits made while save is pending', async()=>{
    let resolve; const s=app(()=>new Promise(r=>resolve=r));
    s.fetchPosts=async()=>{};
    s.acceptPost({id:1,status:'ready',caption:'original',visual_title:'Title'});
    s.currentPost.caption='edit'; const save=s.saveDraft();
    s.currentPost.caption='newer edit';
    resolve({ok:true,json:async()=>({success:true})}); await save;
    assert.equal(s.draftDirty,true);
    assert.equal(s.currentPost.caption,'newer edit');
});
test('regeneration cannot overwrite a different selected post',async()=>{
    let resolve; const s=app(()=>new Promise(r=>resolve=r));
    s.fetchPosts=async()=>{};
    s.acceptPost({id:1,status:'ready',caption:'unsaved',visual_title:'First'});
    const regeneration=s.regenerateImage(1);
    s.acceptPost({id:2,status:'ready',caption:'second',visual_title:'Second',image_url:'second.jpg'});
    resolve({json:async()=>({success:true,image_url:'first-new.jpg'})}); await regeneration;
    assert.equal(s.currentPost.image_url,'second.jpg');
});
test('failed refresh preserves existing data and exposes retry state',async()=>{
    const s=app(async()=>({ok:false})); s.topics=[{id:7}];
    await s.fetchTopics(); assert.equal(s.topics[0].id,7); assert.ok(s.dataErrors.topics);
});
