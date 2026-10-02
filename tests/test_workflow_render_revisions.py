"""Feedback, immutable preview handoff and reusable settings regression cases."""
import asyncio
import copy
import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from easel.content_workflow import ContentWorkflowService, node_of
from easel.content_workflow_api import router
from easel.workflow_chat import updates_for
from easel.workflow_render_settings import pending_render_requests, preferences_for, reusable_settings, revision_props, validate_preferences
from easel.workflow_render_revision import approved_revision
from easel import workflow_runner as module
from tests.test_workflow_render import render_case, install_review_tools


@pytest.mark.parametrize('value', [
    {'playback_rate':float('nan')}, {'playback_rate':0}, {'subtitle_size':97},
    {'subtitle_bottom':.6}, {'bgm_volume':.8}, {'bgm_enabled':'true'},
    {'bgm_track':'../../secret.mp3'}, {'background':'red'}, {'unknown':1},
    {'card_position':{}}, {'bgm_fade_out':float('inf')},
])
def test_preferences_reject_invalid_media_instructions(value):
    with pytest.raises(ValueError):
        validate_preferences(value)


def test_speed_maps_every_clock_once_without_mutating_the_canonical_timeline(render_case):
    _, base, _ = render_case
    before = copy.deepcopy(base)
    prefs = preferences_for(base, {'render_preferences':{'playback_rate':1.1, 'subtitle_size':64, 'subtitle_bottom':.12}})
    revised = revision_props(base, prefs, None)
    assert base == before
    assert revised['duration'] == pytest.approx(20/1.1)
    assert revised['captions'][0]['endMs'] == pytest.approx(2000/1.1)
    assert revised['scenes'][1]['start'] == pytest.approx(10/1.1)
    assert revised['visual']['subtitleSize'] == 64 and revised['visual']['subtitleBottom'] == .12
    assert revision_props(base,prefs,None) == revised
    with pytest.raises(ValueError,match='基线'):
        revision_props(revised,prefs,None)


def test_failed_chat_requirements_survive_and_render_settings_can_be_filled_directly():
    p={'nodes':[{'id':'review','chat':{'messages':[
        {'id':'u1','role':'user','content':'字大一点，1.1倍速，配乐安静','created_at':'1'},
        {'id':'a1','role':'assistant','content':'失败','status':'failed','created_at':'2'},
        {'id':'u2','role':'user','content':'继续，自己找音乐','created_at':'3'}]}}], 'media':{}}
    assert [item['id'] for item in pending_render_requests(p)]==['u1','u2']
    updates=updates_for(p,'review',{'action':'run','updates':{'settings':{'render_preferences':{'bgm_enabled':True,'subtitle_size':64}}}},'字大一点')
    assert updates['settings']['render_preferences']['bgm_enabled'] is True


def test_successful_preview_owns_actual_props_profile_and_untouched_base(render_case,monkeypatch):
    run,base,_=render_case
    base['duration']=30
    base['scenes'][-1]['end']=30
    module.write_json(run.work/'props.json',base)
    before=(run.work/'props.json').read_bytes()
    run.project.update(settings={},nodes=[{'id':'review','version':4}])
    run.options['render_requests']=[{'id':'u1','text':'字号更大更高，1.1倍速'},{'id':'u2','text':'继续'}]
    seen=[]
    async def model(prompt,**kwargs):
        seen.append(prompt)
        return {'preferences':{'subtitle_size':64,'subtitle_bottom':.12,'playback_rate':1.1},'unsupported_requests':[]}
    monkeypatch.setattr(module,'generate',model)
    calls=[]
    install_review_tools(monkeypatch,calls)
    result=asyncio.run(run.execute())
    assert result['status']=='awaiting_review'
    assert '字号更大更高' in seen[0] and '继续' in seen[0]
    receipt=result['render_receipt']
    assert receipt['version']==4 and receipt['preferences']['playback_rate']==1.1
    assert calls[0][1][calls[0][1].index('--props')+1]==Path(receipt['props_path'])
    assert (run.work/'props.json').read_bytes()==before
    profile=next(a for a in result['artifacts'] if a['kind']=='render_profile')
    reusable=json.loads(Path(profile['path']).read_text(encoding='utf-8'))['settings']
    assert reusable['render_preferences']['subtitle_size']==64
    assert all(a['sha256'] and a['version']==4 for a in result['artifacts'] if a['kind']=='preview_video')
    assert 'source' not in str(reusable) and 'manuscript' not in str(reusable)


def test_unimplemented_request_cannot_consume_feedback_or_make_old_video_current(render_case,monkeypatch):
    run,_,_=render_case
    run.options['render_requests']=[{'id':'u1','text':'剪掉第三句并插入新的B-roll'}]
    async def model(*args,**kwargs):
        return {'preferences':{},'unsupported_requests':['删句和B-roll尚未实现']}
    monkeypatch.setattr(module,'generate',model)
    result=asyncio.run(run.execute())
    assert result['status']=='blocked' and '尚未实现' in result['message']
    assert 'render_receipt' not in result
    assert not any(a['kind']=='preview_video' for a in result['artifacts'])


def approved(run,monkeypatch):
    install_review_tools(monkeypatch,[])
    run.project.update(settings={},nodes=[{'id':'review','version':3}])
    result=asyncio.run(run.execute())
    receipt=result['render_receipt']
    run.project['settings']=result['settings']
    run.project['nodes'][0].update(status='completed',approved_version=3,render_receipt=receipt)
    return receipt


@pytest.mark.parametrize('tamper',['props','source','settings','base','approval'])
def test_export_refuses_different_files_or_unconfirmed_revision(render_case,monkeypatch,tamper):
    run,_,_=render_case
    receipt=approved(run,monkeypatch)
    if tamper=='props':Path(receipt['props_path']).write_text('{}')
    elif tamper=='source':(run.work/'public/source.mp4').write_bytes(b'other recording')
    elif tamper=='settings':run.project['settings']['render_preferences']['playback_rate']=1.1
    elif tamper=='base':(run.work/'props.json').write_text('{}')
    else:run.project['nodes'][0]['approved_version']=2
    with pytest.raises((module.RunnerBlocked,KeyError)):
        approved_revision(run)


def test_deliver_edits_update_review_but_do_not_export_an_unapproved_full_video(render_case,monkeypatch):
    run,_,_=render_case
    receipt=approved(run,monkeypatch)
    old=Path(receipt['props_path']).read_bytes()
    run.node='deliver'
    run.options['render_requests']=[{'id':'u-new','text':'字幕再大一点，72'}]
    async def model(*args,**kwargs):return {'preferences':{'subtitle_size':72},'unsupported_requests':[]}
    monkeypatch.setattr(module,'generate',model)
    calls=[]
    install_review_tools(monkeypatch,calls)
    result=asyncio.run(run.execute())
    assert result['status']=='blocked' and 'preview_revision' in result
    preview=result['preview_revision']
    assert preview['render_receipt']['version']==4
    assert preview['render_receipt']['preferences']['subtitle_size']==72
    assert all('final.mp4' not in str(call) for call in calls)
    assert Path(receipt['props_path']).read_bytes()==old


def test_export_chat_without_visual_edits_uses_the_approved_snapshot(render_case,monkeypatch):
    run,_,_=render_case
    receipt=approved(run,monkeypatch)
    run.node='deliver'
    run.options['render_requests']=[{'id':'export','text':'就用这一版，导出全片'}]
    async def model(*args,**kwargs):return {'preferences':{},'unsupported_requests':[]}
    monkeypatch.setattr(module,'generate',model)
    seen=[]
    async def render(self,command,*args,**kwargs):
        seen.append(args)
        Path(args[1]).write_bytes(b'new full export')
    async def probe(*args):return {'duration':20,'width':1080,'height':1920,'audio':True}
    async def command(self,args,**kwargs):
        if '-frames:v' in args:Path(args[-1]).write_bytes(b'cover')
        return 0,''
    monkeypatch.setattr(module._Run,'remotion',render)
    monkeypatch.setattr(module._Run,'probe',probe)
    monkeypatch.setattr(module._Run,'command',command)
    result=asyncio.run(run.execute())
    assert result['status']=='awaiting_review' and 'preview_revision' not in result
    assert len(seen)==1 and seen[0][seen[0].index('--props')+1]==Path(receipt['props_path'])
    assert result['render_receipt']['requests'][-1]['id']=='export'


def test_mixer_new_music_fades_preserve_the_last_voice_samples(tmp_path):
    import shutil, subprocess, wave
    import numpy as np
    if not shutil.which('ffmpeg'):pytest.skip('requires installed ffmpeg')
    voice=tmp_path/'voice.wav';bgm=tmp_path/'bgm.wav';output=tmp_path/'mixed.wav'
    for path,frequency in ((voice,300),(bgm,1000)):
        subprocess.run(['ffmpeg','-nostdin','-y','-v','error','-f','lavfi','-i',f'sine=frequency={frequency}:sample_rate=44100:duration=4',str(path)],check=True)
    script=Path(__file__).resolve().parents[1]/'skills/shared/scripts/audio_mix.py'
    import sys
    subprocess.run([sys.executable,str(script),'mix','--voice',str(voice),'--bgm',str(bgm),'--bgm-volume','.1','--bgm-fade-in','1','--bgm-fade-out','2','--master-fade-out','0','--bgm-loop-off','-o',str(output)],check=True,capture_output=True)
    def samples(path):
        with wave.open(str(path),'rb') as audio:
            return np.frombuffer(audio.readframes(audio.getnframes()),dtype=np.int16).astype(float), audio.getframerate()
    source,sr=samples(voice);mixed,_=samples(output)
    assert len(source)==len(mixed)
    tail=slice(int(sr*3.8),int(sr*3.99))
    assert np.dot(source[tail],mixed[tail])/np.dot(source[tail],source[tail])==pytest.approx(1,abs=.01)
    assert np.std(mixed-source)>0


def test_mixer_calibrates_loud_music_against_a_quiet_recording(tmp_path):
    import shutil, subprocess, sys, wave
    import numpy as np
    if not shutil.which('ffmpeg'):pytest.skip('requires installed ffmpeg')
    voice=tmp_path/'quiet-voice.wav';bgm=tmp_path/'loud-bgm.wav';output=tmp_path/'mixed.wav'
    for path,frequency,volume in ((voice,300,.02),(bgm,1000,1)):
        subprocess.run(['ffmpeg','-nostdin','-y','-v','error','-f','lavfi','-i',f'sine=frequency={frequency}:sample_rate=44100:duration=4,volume={volume}',str(path)],check=True)
    script=Path(__file__).resolve().parents[1]/'skills/shared/scripts/audio_mix.py'
    result=subprocess.run([sys.executable,str(script),'mix','--voice',str(voice),'--bgm',str(bgm),'--bgm-volume','.1',
        '--bgm-relative-to-voice','--bgm-fade-in','1','--bgm-fade-out','1','--master-fade-out','0','--bgm-loop-off','-o',str(output)],check=True,capture_output=True)
    line=next(line for line in result.stdout.decode('utf-8',errors='replace').splitlines() if line.startswith('BGM_CALIBRATION '))
    calibration=json.loads(line.removeprefix('BGM_CALIBRATION '))
    assert 0<calibration['effective_bgm_volume']<.005
    assert calibration['duck_threshold']<.03
    def samples(path):
        with wave.open(str(path),'rb') as audio:
            return np.frombuffer(audio.readframes(audio.getnframes()),dtype=np.int16).astype(float)
    source,mixed=samples(voice),samples(output)
    music=mixed-source
    assert np.sqrt(np.mean(music**2))/np.sqrt(np.mean(source**2))<=.11
    assert np.corrcoef(source,mixed)[0,1]>.99


def test_render_preferences_only_invalidate_review_and_merge_partial_changes(tmp_path):
    service=ContentWorkflowService(tmp_path/'repo',vault=tmp_path/'vault')
    p=service.create({'title':'字幕更大','settings':{'render_preferences':{'playback_rate':1.1,'bgm_enabled':True}}})
    for n in p['nodes']:n.update(status='completed',approved_version=1,version=1)
    service.save(p)
    p=service.patch(p['id'],{'settings':{'render_preferences':{'subtitle_size':72}}})
    assert node_of(p,'build')['status']=='completed'
    assert node_of(p,'review')['status']=='stale'
    assert p['settings']['render_preferences']=={'playback_rate':1.1,'bgm_enabled':True,'subtitle_size':72}


@pytest.mark.parametrize('status',['awaiting_review','blocked'])
def test_only_success_consumes_original_user_requests_and_failure_preserves_old_preview(tmp_path,status):
    class Executor:
        async def execute(self,project,node,options,*args):
            assert [r['id'] for r in options['render_requests']]==['u1','u2']
            if status=='blocked':return {'status':status,'message':'解码失败','artifacts':[]}
            return {'status':status,'message':'新样片','artifacts':[{'path':'new.mp4','kind':'preview_video'}],
                    'render_receipt':{'requests':options['render_requests'],'preferences':{'subtitle_size':64}}}
    service=ContentWorkflowService(tmp_path/'repo',vault=tmp_path/'vault',executor=Executor())
    p=service.create({'title':'原要求不能丢失'})
    node_of(p,'build')['status']='completed'
    review=node_of(p,'review')
    review['artifacts']=[{'path':'old.mp4','kind':'preview_video','version':2}]
    review['chat']={'status':'idle','messages':[{'id':'u1','role':'user','content':'字大一点'},
        {'id':'a1','role':'assistant','status':'failed','content':'未完成'}, {'id':'u2','role':'user','content':'继续'}]}
    service.save(p)
    async def scenario():
        await service.run(p['id'],'review',{})
        await service.tasks[p['id']]
    asyncio.run(scenario())
    saved=service.get(p['id'])
    assert bool(pending_render_requests(saved)) == (status=='blocked')
    paths=[a['path'] for a in node_of(saved,'review')['artifacts']]
    assert paths==(['old.mp4'] if status=='blocked' else ['new.mp4'])


def test_overwritten_artifact_path_changes_media_revision_and_disables_stale_cache(tmp_path):
    service=ContentWorkflowService(tmp_path/'repo',vault=tmp_path/'vault')
    p=service.create({'title':'同名产物'})
    path=service.directory(p['id'])/'artifacts/final.mp4'
    path.parent.mkdir()
    path.write_bytes(b'old')
    node_of(p,'deliver')['artifacts']=[{'name':'最终视频','path':str(path),'kind':'final_video','sha256':module.file_sha256(path)}]
    service.save(p)
    app=FastAPI();app.include_router(router(service));client=TestClient(app)
    first=client.get(f"/api/content-workflows/{p['id']}").json()['nodes'][7]['artifacts'][0]['url']
    path.write_bytes(b'new version')
    second=client.get(f"/api/content-workflows/{p['id']}").json()['nodes'][7]['artifacts'][0]['url']
    assert first != second
    response=client.get(second)
    assert response.content==b'new version' and response.headers['cache-control']=='no-cache'


def test_delivery_preview_handoff_clears_old_approval_and_consumes_only_real_preview_requests(tmp_path):
    class Executor:
        async def execute(self,project,node,options,*args):
            return {'status':'blocked','message':'新样片请确认','artifacts':[],
                'preview_revision':{'message':'字幕更大','artifacts':[{'kind':'preview_video','path':'preview-new.mp4','version':4}],
                'render_receipt':{'version':4,'requests':options['render_requests'],'preferences':{'subtitle_size':64}}}}
    service=ContentWorkflowService(tmp_path/'repo',vault=tmp_path/'vault',executor=Executor())
    p=service.create({'title':'交付意见回到新样片'})
    node_of(p,'build')['status']='completed'
    node_of(p,'review').update(status='completed',version=3,approved_version=3)
    node_of(p,'deliver')['chat']={'status':'idle','messages':[{'id':'u1','role':'user','content':'字再大一点'}]}
    service.save(p)
    async def scenario():
        await service.run(p['id'],'deliver',{})
        await service.tasks[p['id']]
    asyncio.run(scenario())
    saved=service.get(p['id'])
    review=node_of(saved,'review')
    assert review['version']==4 and review['status']=='awaiting_review' and 'approved_version' not in review
    assert review['artifacts'][0]['path']=='preview-new.mp4'
    assert node_of(saved,'deliver')['status']=='blocked'
    assert not pending_render_requests(saved)
