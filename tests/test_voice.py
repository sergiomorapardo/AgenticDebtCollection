"""Verify the local voice channel offline: transport, persistence, and playback gating."""

import json
import socket
from datetime import datetime

import httpx
import pytest

from agents.collections.memory import SQLiteRepository
from agents.collections.runtime import AppContext
from collections_lab.channels.elevenlabs import ElevenLabsSpeech, SpeechError
from collections_lab.channels.voice_session import VoiceSession


def test_speech_requests_use_expected_host_models_and_secret_header(tmp_path):
    seen = []

    def handle(request):
        seen.append(request)
        assert request.url.host == "api.elevenlabs.io"
        assert request.headers["xi-api-key"] == "test-secret"
        assert "test-secret" not in str(request.url)
        if request.url.path.endswith("speech-to-text"):
            assert b"scribe_v2" in request.content
            assert b"fictional-audio" in request.content
            return httpx.Response(200, json={"text": "  quiero tres cuotas  "})
        assert json.loads(request.content)["model_id"] == "eleven_multilingual_v2"
        return httpx.Response(200, content=b"ID3-audio", headers={"content-type": "audio/mpeg"})

    speech = ElevenLabsSpeech("test-secret", "voice-id", transport=httpx.MockTransport(handle))
    assert speech.synthesize("Hola") == b"ID3-audio"
    audio = tmp_path / "recording.wav"
    audio.write_bytes(b"fictional-audio")
    assert speech.transcribe(audio) == "quiero 3 cuotas"
    assert len(seen) == 2


@pytest.mark.parametrize("status", [401, 403, 429, 500])
def test_provider_errors_are_redacted_and_not_retried(status):
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(status, json={"detail": "test-secret private provider body"})

    speech = ElevenLabsSpeech("test-secret", "voice-id", transport=httpx.MockTransport(handle))
    with pytest.raises(SpeechError, match=f"HTTP {status}") as error:
        speech.synthesize("Hola")
    assert "test-secret" not in str(error.value)
    assert len(calls) == 1


def test_invalid_input_and_non_audio_success_fail_closed(tmp_path):
    speech = ElevenLabsSpeech(
        "test-secret",
        "voice-id",
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={})),
    )
    with pytest.raises(SpeechError):
        speech.synthesize("Hola")
    with pytest.raises(SpeechError):
        speech.synthesize(" ")
    with pytest.raises(SpeechError):
        speech.transcribe(tmp_path / "missing.wav")
    audio = tmp_path / "empty.wav"
    audio.write_bytes(b"")
    with pytest.raises(SpeechError):
        speech.transcribe(audio)
    audio.write_bytes(b"x")
    with pytest.raises(SpeechError, match="inválida"):
        speech.transcribe(audio)
    with pytest.raises(ValueError):
        ElevenLabsSpeech("", "voice-id")
    with pytest.raises(ValueError):
        ElevenLabsSpeech("test-secret", "../../other")


class FakeSpeech:
    def __init__(self):
        self.fail = False
        self.calls = []
        self.transcriptions = []

    def synthesize(self, text):
        self.calls.append(text)
        if self.fail:
            raise SpeechError("Audio unavailable")
        return b"ID3-demo"

    def transcribe(self, path):
        self.transcriptions.append(path)
        return "confirmo"


class StreamingSpeech(FakeSpeech):
    async def stream_synthesize(self, text):
        self.calls.append(text)
        yield b"\x01\x00"
        if self.fail:
            raise SpeechError("private upstream failure")
        yield b"\x02\x00"


def test_elevenlabs_stream_delivers_first_chunk_before_completion_and_closes():
    import asyncio

    closed = []

    class Chunks(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"\x01\x00"
            raise AssertionError("Consumer must not buffer the whole response")

        async def aclose(self):
            closed.append(True)

    def handle(request):
        assert request.url.path.endswith("/voice-id/stream")
        assert request.url.params["output_format"] == "pcm_24000"
        payload = json.loads(request.content)
        assert payload["model_id"] == "eleven_multilingual_v2"
        assert payload["text"] == "seiscientos mil pesos"
        return httpx.Response(200, stream=Chunks(), headers={"content-type": "audio/pcm"})

    async def run():
        speech = ElevenLabsSpeech("test-key", "voice-id", transport=httpx.MockTransport(handle))
        source = speech.stream_synthesize("600,000 COP")
        assert await anext(source) == b"\x01\x00"
        await source.aclose()

    asyncio.run(run())
    assert closed == [True]


@pytest.mark.parametrize(
    ("written", "spoken"),
    [
        ("El saldo es 600,000 COP.", "El saldo es seiscientos mil pesos."),
        ("Puedes pagar 600000 COP.", "Puedes pagar seiscientos mil pesos."),
        ("Saldo: $600.000 COP.", "Saldo: seiscientos mil pesos."),
        ("Total: COP 1,200,000.", "Total: un millón doscientos mil pesos."),
        ("1 COP y 20 cop.", "un peso y veinte pesos."),
        ("200000 COP 3 cuotas", "doscientos mil pesos 3 cuotas"),
        ("Importes en COP; COPIA del acuerdo.", "Importes en pesos; COPIA del acuerdo."),
        ("0 COP", "cero pesos"),
        ("21 COP", "veintiún pesos"),
        ("31 COP", "treinta y un pesos"),
        ("100 COP", "cien pesos"),
        ("101 COP", "ciento un pesos"),
        ("1001 COP", "mil un pesos"),
        ("1000000 COP", "un millón de pesos"),
        ("21000000 COP", "veintiún millones de pesos"),
        ("1000000000 COP", "mil millones de pesos"),
        ("1000000000000 COP", "un billón de pesos"),
        ("1,01 COP", "un peso con un centavo"),
        ("600.000 COP y 200.000 COP", "seiscientos mil pesos y doscientos mil pesos"),
        ("Saldo: 12,50 COP.", "Saldo: doce pesos con cincuenta centavos."),
        ("Saldo: 1.000 COP.", "Saldo: mil pesos."),
        ("Saldo: $1 COP.", "Saldo: un peso."),
        ("Saldo: 12,50 cop.", "Saldo: doce pesos con cincuenta centavos."),
        ("Cuota 200\u00a0COP el 2026-10-20.", "Cuota doscientos pesos el 2026-10-20."),
    ],
)
def test_currency_is_spoken_as_pesos_in_both_adapters(written, spoken):
    import asyncio

    seen = []

    def handle(request):
        seen.append(json.loads(request.content)["text"])
        return httpx.Response(200, content=b"\x01\x00", headers={"content-type": "audio/pcm"})

    speech = ElevenLabsSpeech("test-key", "voice-id", transport=httpx.MockTransport(handle))
    speech.synthesize(written)

    async def consume():
        return b"".join([chunk async for chunk in speech.stream_synthesize(written)])

    assert asyncio.run(consume()) == b"\x01\x00"
    assert seen == [spoken, spoken]


def test_spoken_integer_amounts_round_trip_through_independent_number_parser():
    from agents.collections.numbers import number_value
    from collections_lab.channels.elevenlabs import _spanish_integer

    for value in [*range(1000), 1001, 21000, 101001, 600000, 1000001, 2000021, 999999999]:
        assert number_value(_spanish_integer(value)) == value


@pytest.mark.parametrize("written", ["1.234.56 COP", "1000000000000000000 COP"])
def test_ambiguous_or_unsupported_currency_fails_before_paid_request(written):
    def handle(request):
        raise AssertionError("Unsupported amounts must not reach the provider")

    speech = ElevenLabsSpeech("test-key", "voice-id", transport=httpx.MockTransport(handle))
    with pytest.raises(SpeechError):
        speech.synthesize(written)


def test_spoken_currency_does_not_change_written_offer_or_history(session):
    import asyncio
    from pathlib import Path

    offer(session)
    written = json.dumps({"reply": session.reply, "history": session.history})
    assert "200,000 COP" in written
    sent = []

    def handle(request):
        sent.append(json.loads(request.content)["text"])
        return httpx.Response(200, content=b"\x01\x00", headers={"content-type": "audio/pcm"})

    session.speech = ElevenLabsSpeech("test-key", "voice-id", transport=httpx.MockTransport(handle))
    audio_id = Path(session.retry_audio()).name
    session.claim_audio(audio_id)

    async def consume():
        return b"".join([chunk async for chunk in session.stream_audio(audio_id)])

    assert asyncio.run(consume()) == b"\x01\x00"
    assert "doscientos mil pesos" in sent[0] and "COP" not in sent[0]
    assert json.dumps({"reply": session.reply, "history": session.history}) == written
    assert not session.context.repository.agreements()


def test_streaming_api_replays_completed_audio_without_charges(session, tmp_path):
    from fastapi.testclient import TestClient

    from collections_lab.channels.voice_app import create_app

    speech = StreamingSpeech()
    with TestClient(
        create_app(speech, data_dir=tmp_path / "stream-api", now=session.context.now),
        base_url="http://127.0.0.1",
    ) as client:
        client.get("/")
        state = api_post(client, "start").json()
        assert state["audio_format"] == "pcm_s16le_24000" and not speech.calls
        assert (
            api_post(
                client, "playback", state=state, payload={"audio_id": state["audio_id"]}
            ).status_code
            == 400
        )
        assert client.get(state["audio_url"]).content == b"\x01\x00\x02\x00"
        assert client.get(state["audio_url"]).content == b"\x01\x00\x02\x00"
        assert len(speech.calls) == 1
        assert finish_playback(client, state)["ready"]
        assert client.get("/api/session").json()["details"]["audio_status"] == "complete"


def test_partial_stream_cannot_acknowledge_or_implicitly_retry(session, tmp_path):
    from fastapi.testclient import TestClient

    from collections_lab.channels.voice_app import create_app

    speech = StreamingSpeech()
    speech.fail = True
    with TestClient(
        create_app(speech, data_dir=tmp_path / "stream-api", now=session.context.now),
        base_url="http://127.0.0.1",
    ) as client:
        client.get("/")
        state = api_post(client, "start").json()
        with pytest.raises(SpeechError, match="audio no terminó"):
            client.get(state["audio_url"])
        current = client.get("/api/session").json()
        assert current["details"]["audio_status"] == "error"
        assert "private" not in json.dumps(current)
        assert (
            api_post(
                client, "playback", state=state, payload={"audio_id": state["audio_id"]}
            ).status_code
            == 400
        )
        assert client.get(state["audio_url"]).status_code == 409
        assert len(speech.calls) == 1
        recovered = api_post(client, "retry", state=state).json()
        assert recovered["audio_id"] != state["audio_id"]
        assert recovered["history"] == state["history"]
        speech.fail = False
        assert client.get(recovered["audio_url"]).status_code == 200
        assert finish_playback(client, recovered)["ready"]


def test_interrupted_offer_acceptance_does_not_write_agreement_but_counteroffer_works(session):
    from pathlib import Path

    offer(session)
    session.interrupt_audio(Path(session.audio).name)
    assert not session.acknowledge_playback(session.audio)
    session.send("confirmo")
    assert not session.context.repository.agreements()
    assert session.context.offer_audio_completed is False
    session.interrupt_audio(Path(session.audio).name)
    reply = session.send("quiero 2 cuotas desde 2026-10-21")
    assert reply["stage"] == "awaiting_confirmation"
    assert session.acknowledge_playback(session.audio)
    session.send("confirmo")
    assert len(session.context.repository.agreements()) == 1


def test_playing_history_does_not_acknowledge_an_interrupted_offer(session):
    from pathlib import Path

    offer(session)
    session.interrupt_audio(Path(session.audio).name)
    session.send("qué acordamos antes")
    assert session.acknowledge_playback(session.audio)
    session.send("confirmo")
    assert session.context.offer_audio_completed is False
    assert not session.context.repository.agreements()


def test_interrupted_offer_is_represented_before_a_new_acceptance(session):
    from pathlib import Path

    original = offer(session)
    session.interrupt_audio(Path(session.audio).name)
    replay = session.send("confirmo")
    assert replay["stage"] == "awaiting_confirmation"
    assert replay["offer_id"] == original["offer_id"]
    assert replay["text"].endswith(original["text"])
    assert "Escúchala completa" in replay["text"]
    assert not session.context.repository.agreements()
    assert session.acknowledge_playback(session.audio)
    session.interrupt_audio(Path(session.audio).name)  # A late interrupt cannot revoke completion.
    assert session.audio_status == "complete" and session.played_audio == session.audio
    session.send("confirmo")
    assert len(session.context.repository.agreements()) == 1


def test_interrupt_cancels_inflight_provider_and_rejects_stale_audio(session):
    import asyncio
    from pathlib import Path

    closed = []

    class SlowSpeech(StreamingSpeech):
        async def stream_synthesize(self, text):
            try:
                yield b"\x01\x00"
                await asyncio.sleep(60)
            finally:
                closed.append(True)

    async def run():
        session.speech = SlowSpeech()
        session.send("Hola")
        audio_id = Path(session.audio).name
        session.claim_audio(audio_id)
        started = asyncio.Event()

        async def consume():
            async for _ in session.stream_audio(audio_id):
                started.set()

        task = asyncio.create_task(consume())
        await started.wait()
        with pytest.raises(ValueError):
            session.claim_audio(audio_id)
        session.interrupt_audio(audio_id)
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
        assert closed == [True]
        assert session.audio_status == "interrupted"
        assert not session.acknowledge_playback(audio_id)
        session.retry_audio()
        with pytest.raises(ValueError):
            session.interrupt_audio(audio_id)
        assert not list(session.directory.glob("*.part"))

    asyncio.run(run())


def test_asgi_interrupt_closes_stream_from_another_request(session, tmp_path):
    import asyncio
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from fastapi.testclient import TestClient

    from collections_lab.channels.voice_app import create_app

    started, closed = Event(), Event()

    class SlowSpeech(StreamingSpeech):
        async def stream_synthesize(self, text):
            try:
                started.set()
                yield b"\x01\x00"
                await asyncio.sleep(60)
            finally:
                closed.set()

    with (
        TestClient(
            create_app(SlowSpeech(), data_dir=tmp_path / "asgi", now=session.context.now),
            base_url="http://127.0.0.1",
            raise_server_exceptions=False,
        ) as client,
        ThreadPoolExecutor() as pool,
    ):
        client.get("/")
        state = api_post(client, "start").json()
        future = pool.submit(client.get, state["audio_url"])
        assert started.wait(2)
        response = api_post(
            client, "interrupt", state=state, payload={"audio_id": state["audio_id"]}
        )
        assert response.status_code == 200 and response.json()["ready"]
        assert closed.wait(2)
        future.result(timeout=2)
        assert client.get("/api/session").json()["details"]["audio_status"] == "interrupted"


def test_dictated_number_is_normalized_in_saved_transcript(session, monkeypatch):
    session.send("Hola")
    session.send("123456789")
    monkeypatch.setattr(session.speech, "transcribe", lambda _: "uno uno uno uno")
    session.respond_to_audio("test.wav")
    assert session.history[-2]["content"] == "1111"
    assert session.reply["stage"] == "ready"


def test_comma_separated_demo_id_is_found(session, monkeypatch):
    session.send("Hola")
    monkeypatch.setattr(
        session.speech,
        "transcribe",
        lambda _: "Mi cédula es 1, 2, 3, 4, 5, 6, 7, 8, 9",
    )
    session.respond_to_audio("test.wav")
    assert session.history[-2]["content"] == "Mi cédula es 123456789"
    assert session.reply["stage"] == "awaiting_identity"
    assert session.send("verificar 1111")["stage"] == "ready"


def test_browser_native_wav_barge_in_and_recording_preroll(tmp_path):
    """Exercise the shipped browser code with controlled audio/network timing in Node."""
    import shutil
    import subprocess
    from pathlib import Path

    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required for browser state-machine regression tests")
    runner = tmp_path / "voice.cjs"
    runner.write_text(r"""
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const html=fs.readFileSync(process.argv[2],'utf8'),full=html.split('<script>')[1].split('</script>')[0];
new vm.Script(full);
const script=full.split('// The viewer')[0].replace('restoreSession();','');
function fixture({failure=false,interruptFailure=false,bytes=[1,0,255,127,0,128,255,255],blocked=false}={}){
 const calls=[],audios=[],blobs=new Map(),revoked=[],timers=new Map(),elements=new Map();let eof=false,clock=1,rms=0,aborted=false,reads=0;
 const element=()=>({dataset:{},classList:{toggle(){}},append(){},replaceChildren(){},setAttribute(){},focus(){},querySelector(){return element();},value:'',textContent:''});
 const document={documentElement:{dataset:{}},getElementById(id){if(!elements.has(id))elements.set(id,element());return elements.get(id);},createElement:element,createTextNode:text=>text};
 class Context{constructor(){this.state='running';}resume(){return Promise.resolve();}createBufferSource(){throw new Error('Must use native playback');}createMediaStreamSource(){return {connect(){},disconnect(){}};}createAnalyser(){return {fftSize:2048,disconnect(){},getFloatTimeDomainData(data){data.fill(rms);}};}}
 class Recorder{static isTypeSupported(){return true;}constructor(){this.state='inactive';}start(){this.state='recording';}stop(){this.state='inactive';setImmediate(()=>this.onstop?.());}}
 class Audio{constructor(url){this.url=url;audios.push(this);}async play(){if(blocked)throw new Error('autoplay blocked');this.playing=true;}pause(){this.paused=true;}removeAttribute(){}load(){}end(){this.ended=true;this.onended?.();}}
 let releaseEnd;const end=new Promise(resolve=>{releaseEnd=()=>{eof=true;resolve({done:true});};});
 const sandbox={console,document,Audio,AudioContext:Context,MediaRecorder:Recorder,Blob,AbortController,Uint8Array,Float32Array,ArrayBuffer,DataView,Date,crypto:require('node:crypto').webcrypto,URL:{createObjectURL(blob){const url='blob:'+blobs.size;blobs.set(url,blob);return url;},revokeObjectURL(url){revoked.push(url);}},
 performance:{now:()=>clock},navigator:{mediaDevices:{getUserMedia:async()=>({getTracks:()=>[{stop(){}}]})}},window:{MediaRecorder:Recorder,addEventListener(){}},setTimeout,clearTimeout,setInterval(fn,ms){timers.set(ms,fn);return ms;},clearInterval:id=>timers.delete(id),
 fetch:async(url,options={})=>{
  if(url.startsWith('/api/audio/')){let index=0;return {ok:true,body:{getReader:()=>({read:async()=>{reads++;if(index++===0)return {value:new Uint8Array(bytes.slice(0,1)),done:false};if(index===2)return {value:new Uint8Array(bytes.slice(1)),done:false};if(failure)throw new Error('truncated');return Promise.race([end,new Promise((_,reject)=>{const abort=()=>{aborted=true;reject(new Error('aborted'));};if(options.signal.aborted)abort();else options.signal.addEventListener('abort',abort,{once:true});})]);}})}};}
  calls.push({url,body:options.body,headers:options.headers});
  if(url==='/api/interrupt'&&interruptFailure){interruptFailure=false;return {ok:false,json:async()=>({detail:'temporary interrupt error'})};}
  if(url==='/api/playback'){assert.ok(eof);assert.ok(audios.at(-1).ended,'must wait for native ended event');}
  return {ok:true,json:async()=>({session_id:'call',audio_id:'current.pcm',audio_format:'pcm_s16le_24000',ready:true,history:[],finished:false,details:{}})};
 }};
 const context=vm.createContext(sandbox);vm.runInContext(script,context);const run=code=>vm.runInContext(code,context);
 run("state={session_id:'call',audio_id:'current.pcm',audio_url:'/api/audio/current.pcm',audio_format:'pcm_s16le_24000',finished:false,history:[]};epoch=1;");
 return {run,calls,audios,blobs,revoked,releaseEnd,get reads(){return reads;},get aborted(){return aborted;},tick(value){clock+=70;rms=value;timers.get(30)?.();},close(){run('releaseMic();stopPlayback();');}};
}
const until=async predicate=>{for(let i=0;i<100;i++){if(predicate())return;await new Promise(resolve=>setTimeout(resolve,5));}throw new Error('Timed out');};
(async()=>{
 const first=fixture(),loading=first.run('playResponse(epoch)');await until(()=>first.reads>=3);
 assert.equal(first.audios.length,0,'no partial audio playback');assert.equal(first.calls.length,0);
 first.releaseEnd();await loading;assert.equal(first.run('mode'),'speaking');
 const wav=await first.blobs.values().next().value.arrayBuffer(),view=new DataView(wav);
 assert.equal(Buffer.from(wav).subarray(0,4).toString(),'RIFF');assert.equal(Buffer.from(wav).subarray(8,12).toString(),'WAVE');
 assert.equal(view.getUint32(4,true),44);assert.equal(view.getUint16(20,true),1);assert.equal(view.getUint16(22,true),1);assert.equal(view.getUint32(24,true),24000);assert.equal(view.getUint32(28,true),48000);assert.equal(view.getUint16(32,true),2);assert.equal(view.getUint16(34,true),16);assert.equal(view.getUint32(40,true),8);
 assert.deepEqual(Array.from(new Uint8Array(wav,44)),[1,0,255,127,0,128,255,255],'all samples retained with no tones or silence');
 assert.equal(first.calls.length,0);await first.run('prepareMic()');first.run("capture.recorder.ondataavailable({data:new Blob(['initial syllable'])});");
 const oldEnded=first.audios[0].onended;first.tick(.1);first.tick(.1);first.tick(.1);
 assert.equal(first.run('mode'),'recording');assert.ok(first.audios[0].paused);assert.equal(first.revoked.length,1);oldEnded();
 assert.equal(first.calls.filter(c=>c.url==='/api/playback').length,0);first.run('finishRecording()');await until(()=>first.calls.some(c=>c.url==='/api/recording'));
 assert.equal(await first.calls.find(c=>c.url==='/api/recording').body.text(),'initial syllable');first.close();
 const complete=fixture(),ready=complete.run('playResponse(epoch)');complete.releaseEnd();await ready;complete.audios[0].end();await until(()=>complete.run('mode')==='ready');assert.equal(complete.calls.filter(c=>c.url==='/api/playback').length,1);assert.equal(complete.revoked.length,1);complete.close();
 for(const options of [{failure:true},{bytes:[]},{bytes:[1]}]){const broken=fixture(options),p=broken.run('playResponse(epoch)');broken.releaseEnd();await p;assert.equal(broken.audios.length,0);assert.equal(broken.calls.length,0);assert.equal(broken.run('mode'),'waiting');broken.close();}
 const cancelled=fixture(),download=cancelled.run('playResponse(epoch)');await until(()=>cancelled.reads>=3);cancelled.run('epoch++;stopPlayback();');await download;assert.ok(cancelled.aborted);assert.equal(cancelled.audios.length,0);assert.equal(cancelled.calls.length,0);cancelled.close();
 const pending=fixture();pending.run('let finishMic;micPending=new Promise(resolve=>{finishMic=resolve;});');const pendingPlayback=pending.run('playResponse(epoch)');pending.releaseEnd();await until(()=>pending.run('mode')==='waiting');assert.equal(pending.audios.length,0);pending.run('finishMic();');await pendingPlayback;assert.equal(pending.audios.length,1);pending.close();
 const stale=fixture();stale.run('let finishMic;micPending=new Promise(resolve=>{finishMic=resolve;});');const stalePlayback=stale.run('playResponse(epoch)');stale.releaseEnd();await until(()=>stale.run('mode')==='waiting');stale.run('epoch++;stopPlayback();releaseMic();finishMic();');await stalePlayback;assert.equal(stale.audios.length,0);assert.equal(stale.blobs.size,0);
 const denied=fixture();denied.run("micPending=Promise.reject(new Error('permission denied'));micPending.catch(()=>{});");const deniedPlayback=denied.run('playResponse(epoch)');denied.releaseEnd();await deniedPlayback;assert.equal(denied.run('mode'),'speaking');denied.close();
 const blocked=fixture({blocked:true}),blockedPlayback=blocked.run('playResponse(epoch)');blocked.releaseEnd();await blockedPlayback;assert.equal(blocked.run('mode'),'waiting');assert.equal(blocked.calls.length,0);assert.equal(blocked.revoked.length,1);blocked.close();
 const failed=fixture(),failedPlayback=failed.run('playResponse(epoch)');failed.releaseEnd();await failedPlayback;failed.audios[0].onerror();assert.equal(failed.run('mode'),'waiting');assert.equal(failed.calls.length,0);assert.equal(failed.revoked.length,1);failed.close();
 const retry=fixture({interruptFailure:true}),retryPlayback=retry.run('playResponse(epoch)');retry.releaseEnd();await retryPlayback;await retry.run('prepareMic()');retry.run("capture.recorder.ondataavailable({data:new Blob(['uno'])});beginRecording();finishRecording();");await until(()=>retry.run('mode')==='waiting');retry.run('recovery()');await until(()=>retry.calls.some(c=>c.url==='/api/recording'));const interrupts=retry.calls.filter(c=>c.url==='/api/interrupt');assert.equal(interrupts.length,2);assert.equal(interrupts[0].headers['X-Request-ID'],interrupts[1].headers['X-Request-ID']);retry.close();
 console.log('Native WAV: exact samples, complete download, interruption, cancellation, mic races, autoplay recovery, ended-only acknowledgment passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
""")
    result = subprocess.run(
        [
            node,
            str(runner),
            str(Path(__file__).parents[1] / "src/collections_lab/channels/voice.html"),
        ],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.fixture
def session(tmp_path, monkeypatch):
    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "false")

    def no_network(*args, **kwargs):
        raise AssertionError("Voice tests must not call paid services")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    context = AppContext(
        SQLiteRepository(tmp_path / "conversation.sqlite3"),
        datetime.fromisoformat("2026-10-19T09:00:00-05:00"),
    )
    return VoiceSession(context, FakeSpeech(), tmp_path / "audio")


def offer(session, customer="1000000001"):
    session.send("Hola")
    identified = session.send(f"Mi cédula es {customer}")
    assert identified["stage"] == "awaiting_identity"
    assert "600,000" not in identified["text"]
    session.send("verificar 1111")
    reply = session.send("quiero 3 cuotas desde 2026-10-20")
    assert reply["stage"] == "awaiting_confirmation"
    return reply


@pytest.mark.parametrize("customer", ["1000000001", "123456789"])
def test_confirmation_requires_acknowledgement_for_current_offer(session, customer):
    proposal = offer(session, customer)
    assert session.transcribe("synthetic.wav") == "confirmo"
    assert not session.context.repository.agreements()  # STT never runs the graph.
    with pytest.raises(ValueError, match="Espera a que termine"):
        session.send("confirmo")
    assert not session.context.repository.agreements()
    assert session.acknowledge_playback(session.audio)
    assert session.respond_to_audio("synthetic.wav")["stage"] == "agreed"
    assert len(session.context.repository.agreements()) == 1
    assert session.context.repository.get_account("demo-001").balance_cop == 600000
    evidence = json.loads((session.directory / "playback-acknowledgements.jsonl").read_text())
    assert evidence["offer_id"] == proposal["offer_id"]
    with pytest.raises(ValueError, match="terminó"):
        session.send("confirmo")
    assert len(session.context.repository.agreements()) == 1


def test_audio_failure_preserves_turn_and_retry_does_not_repeat_business_write(session):
    offer(session)
    session.speech.fail = True
    session.acknowledge_playback(session.audio)
    session.send("confirmo")
    assert len(session.context.repository.agreements()) == 1
    assert session.audio is None
    assert "turno ya está guardado" in session.warning
    history = list(session.history)
    session.speech.fail = False
    assert session.retry_audio()
    assert session.history == history
    assert len(session.context.repository.agreements()) == 1


def test_failed_offer_audio_cannot_be_acknowledged_but_can_be_ended(session):
    session.send("Hola")
    session.send("1000000001")
    session.send("1111")
    session.speech.fail = True
    session.send("quiero 3 cuotas desde 2026-10-20")
    with pytest.raises(ValueError, match="Espera a que termine"):
        session.send("confirmo")
    assert session.end_call()["finished"]
    assert not session.context.repository.agreements()


def test_acknowledgement_does_not_carry_over_to_a_changed_offer(session):
    first = offer(session)
    previous_audio = session.audio
    session.acknowledge_playback(previous_audio)
    second = session.send("quiero 2 cuotas desde 2026-10-21")
    assert not session.acknowledge_playback(previous_audio)
    assert first["offer_id"] != second["offer_id"]
    with pytest.raises(ValueError, match="Espera a que termine"):
        session.send("confirmo")
    assert not session.context.repository.agreements()


def test_new_session_has_no_access_to_previous_identity(session, tmp_path):
    offer(session)
    other = VoiceSession(session.context, FakeSpeech(), tmp_path / "other")
    reply = other.send("Qué acordamos")
    assert reply["stage"] == "awaiting_customer"
    assert "600,000" not in reply["text"]


def test_retry_and_stale_playback_do_not_unlock_new_offer_audio(session):
    offer(session)
    previous_audio = session.audio
    assert session.acknowledge_playback(previous_audio)
    session.retry_audio()
    assert not session.acknowledge_playback(previous_audio)
    with pytest.raises(ValueError, match="Espera a que termine"):
        session.respond_to_audio("blocked.wav")
    assert not session.speech.transcriptions
    assert not session.context.repository.agreements()
    assert session.acknowledge_playback(session.audio)
    session.respond_to_audio("confirm.wav")
    assert len(session.context.repository.agreements()) == 1


def test_transcription_failure_never_advances_the_business_turn(session, monkeypatch):
    session.send("Hola")
    history = list(session.history)

    def fail(path):
        raise SpeechError("No se detectó voz")

    monkeypatch.setattr(session.speech, "transcribe", fail)
    with pytest.raises(SpeechError):
        session.respond_to_audio("empty.wav")
    assert session.history == history
    assert len(session.speech.calls) == 1


@pytest.mark.parametrize("text", ["91234567890", "1234567890", "123456789 y 1000000002"])
def test_unknown_or_ambiguous_customer_ids_do_not_select_an_account(session, text):
    reply = session.send(text)
    assert reply["stage"] == "awaiting_customer"
    assert "600,000" not in reply["text"]
    assert not session.context.repository.agreements()


@pytest.fixture
def voice_api(session, tmp_path):
    from fastapi.testclient import TestClient

    from collections_lab.channels.voice_app import create_app

    app = create_app(session.speech, data_dir=tmp_path / "api", now=session.context.now)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        response = client.get("/")
        assert response.status_code == 200
        assert "HttpOnly" in response.headers["set-cookie"]
        assert not session.speech.calls
        yield client


def api_post(client, action, *, state=None, payload=None, key=None, content=None):
    from uuid import uuid4

    headers = {
        "X-Voice-Client": "1",
        "X-Request-ID": key or uuid4().hex,
        "X-Call-ID": state["session_id"] if state else "",
    }
    if content is not None:
        return client.post(
            "/api/" + action, content=content, headers={**headers, "Content-Type": "audio/webm"}
        )
    return client.post("/api/" + action, json=payload or {}, headers=headers)


def finish_playback(client, state):
    response = api_post(client, "playback", state=state, payload={"audio_id": state["audio_id"]})
    assert response.status_code == 200
    return response.json()


def test_voice_api_session_audio_isolation_and_request_replay(voice_api, session):
    from fastapi.testclient import TestClient

    key = "a" * 32
    first = api_post(voice_api, "start", key=key)
    assert first.status_code == 200
    state = first.json()
    assert len(session.speech.calls) == 1
    assert api_post(voice_api, "start", key=key).json() == state
    assert len(session.speech.calls) == 1
    assert voice_api.get(state["audio_url"]).content == b"ID3-demo"
    with TestClient(voice_api.app, base_url="http://127.0.0.1") as other:
        assert other.get(state["audio_url"]).status_code == 401
        other.get("/")
        assert other.get("/api/session").json()["session_id"] is None
        assert other.get(state["audio_url"]).status_code == 404
    assert "test-secret" not in first.text
    assert "/api/audio/" in state["audio_url"]
    assert (
        api_post(voice_api, "text", state=state, key=key, payload={"text": "Hola"}).status_code
        == 409
    )


def test_voice_api_recording_is_one_turn_and_hangup_has_no_speech_cost(voice_api, session):
    state = api_post(voice_api, "start").json()
    assert api_post(voice_api, "recording", state=state, content=b"demo").status_code == 400
    assert not session.speech.transcriptions
    state = finish_playback(voice_api, state)
    key = "b" * 32
    response = api_post(voice_api, "recording", state=state, content=b"demo", key=key)
    assert response.status_code == 200
    assert len(response.json()["history"]) == 4
    assert len(session.speech.transcriptions) == 1
    assert (
        api_post(voice_api, "recording", state=state, content=b"demo", key=key).json()
        == response.json()
    )
    assert len(session.speech.transcriptions) == 1
    assert (
        api_post(voice_api, "recording", state=state, content=b"changed", key=key).status_code
        == 409
    )
    count = len(session.speech.calls)
    closed = api_post(voice_api, "end", state=response.json()).json()
    assert closed["finished"] and closed["audio_id"] is None
    assert len(session.speech.calls) == count
    assert not list(session.directory.parent.rglob("input-*"))


def test_voice_api_nine_digit_id_offer_and_stale_playback(voice_api):
    state = api_post(voice_api, "start").json()
    for text in ["Mi cédula es 123456789", "verificar 1111", "quiero 3 cuotas desde 2026-10-20"]:
        state = finish_playback(voice_api, state)
        response = api_post(voice_api, "text", state=state, payload={"text": text})
        assert response.status_code == 200
        state = response.json()
    assert state["details"]["stage"] == "awaiting_confirmation"
    names = [event["name"] for event in voice_api.get("/api/trace").json()["events"]]
    assert "negotiation" in names and "commitment" not in names
    assert api_post(voice_api, "text", state=state, payload={"text": "confirmo"}).status_code == 400
    old_audio = state["audio_id"]
    state = api_post(voice_api, "retry", state=state).json()
    assert (
        api_post(voice_api, "playback", state=state, payload={"audio_id": old_audio}).status_code
        == 400
    )
    state = finish_playback(voice_api, state)
    result = api_post(voice_api, "text", state=state, payload={"text": "confirmo"}).json()
    assert result["finished"] and result["details"]["agreement_id"]
    names = [event["name"] for event in voice_api.get("/api/trace").json()["events"]]
    assert "confirmation" in names and "commitment" in names
    new_call = api_post(voice_api, "start").json()
    assert new_call["session_id"] != result["session_id"]
    assert api_post(voice_api, "end", state=result).status_code == 400


def test_voice_api_rejects_cross_origin_invalid_and_oversized_requests(voice_api, session):
    from uuid import uuid4

    headers = {"X-Voice-Client": "1", "X-Request-ID": uuid4().hex}
    assert voice_api.post("/api/start", json={}).status_code == 403
    assert (
        voice_api.post(
            "/api/start", json={}, headers={**headers, "Origin": "https://other.example"}
        ).status_code
        == 403
    )
    assert (
        voice_api.post(
            "/api/start", json={}, headers={**headers, "Host": "other.example"}
        ).status_code
        == 400
    )
    assert voice_api.post("/api/start", content="[", headers=headers).status_code == 400
    assert voice_api.post("/api/recording", content=b"x", headers=headers).status_code == 415
    assert (
        voice_api.post(
            "/api/recording",
            content=b"x" * (10 * 1024 * 1024 + 1),
            headers={**headers, "Content-Type": "audio/webm"},
        ).status_code
        == 413
    )
    assert not session.speech.calls


def test_voice_api_clock_survives_reload_and_stops_after_hangup(voice_api, monkeypatch):
    import collections_lab.channels.voice_app as module

    clock = [1000.0]
    monkeypatch.setattr(module, "monotonic", lambda: clock[0])
    state = api_post(voice_api, "start").json()
    clock[0] += 15
    assert voice_api.get("/api/session").json()["elapsed_seconds"] == 15
    closed = api_post(voice_api, "end", state=state).json()
    clock[0] += 30
    assert voice_api.get("/api/session").json()["elapsed_seconds"] == closed["elapsed_seconds"]


def test_voice_api_caches_uncertain_failure_without_repeating_turn(voice_api, session, monkeypatch):
    state = finish_playback(voice_api, api_post(voice_api, "start").json())
    attempts = []

    def fail(*args, **kwargs):
        attempts.append(True)
        raise RuntimeError("unexpected provider failure")

    monkeypatch.setattr(session.speech, "synthesize", fail)
    key = "c" * 32
    response = api_post(voice_api, "text", state=state, payload={"text": "123456789"}, key=key)
    assert response.status_code == 500
    assert "unexpected provider" not in response.text
    replay = api_post(voice_api, "text", state=state, payload={"text": "123456789"}, key=key)
    assert replay.json() == response.json()
    assert len(attempts) == 1
    assert len(voice_api.get("/api/session").json()["history"]) == 4


def test_execution_trace_matches_graph_and_excludes_customer_content(session):
    session.send("Mi cédula es 123456789")
    trace = session.trace.snapshot()
    assert trace["turn"] == 1
    assert set(trace["topology"]["nodes"]) == set(session.graph.get_graph().nodes)
    assert [event["name"] for event in trace["events"]] == [
        "human_input",
        "understand_message",
        "route_chat_message",
        "identify_customer",
        "assistant_message",
        "text_to_speech",
    ]
    assert all(event["status"] == "complete" for event in trace["events"])
    assert all(event["duration_ms"] >= 0 for event in trace["events"])
    serialized = json.dumps(trace)
    for private in ("123456789", "Mi cédula", "1111", "test-secret", "demo-001"):
        assert private not in serialized
    assert "started" not in serialized
    session.send("1111")
    assert session.trace.snapshot()["turn"] == 2
    assert "verify_identity" in [event["name"] for event in session.trace.snapshot()["events"]]


def test_trace_audio_retry_and_failures_do_not_replay_nodes(session, monkeypatch):
    session.send("Hola")
    original = session.trace.snapshot()["events"]
    session.speech.fail = True
    session.retry_audio()
    failed = session.trace.snapshot()["events"]
    assert failed[:-1] == original
    assert failed[-1]["name"] == "text_to_speech"
    assert failed[-1]["status"] == "error"

    def fail(path):
        raise SpeechError("private-transcript-secret")

    monkeypatch.setattr(session.speech, "transcribe", fail)
    with pytest.raises(SpeechError):
        session.respond_to_audio("test.wav")
    events = session.trace.snapshot()["events"]
    assert len(events) == 1 and events[0]["status"] == "error"
    assert events[0]["name"] == "speech_to_text"
    assert "private-transcript-secret" not in json.dumps(events)


def test_trace_is_readable_during_a_running_node_and_scoped_to_browser(voice_api, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from fastapi.testclient import TestClient
    from langgraph.graph import END, START, StateGraph

    import collections_lab.channels.voice_session as module

    entered, release = Event(), Event()

    def blocked_node(state):
        entered.set()
        assert release.wait(5)
        return {"reply": {"text": "Synthetic response", "finished": False, "stage": "ready"}}

    builder = StateGraph(dict)
    builder.add_node("blocked_node", blocked_node)
    builder.add_edge(START, "blocked_node")
    builder.add_edge("blocked_node", END)
    graph = builder.compile()
    monkeypatch.setattr(module, "build_graph", lambda **kwargs: graph)
    initial = voice_api.get("/api/trace").json()
    assert not initial["events"] and initial["session_id"] is None
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = pool.submit(api_post, voice_api, "start", key="d" * 32)
        try:
            assert entered.wait(3)
            snapshot = pool.submit(voice_api.get, "/api/trace").result(timeout=2).json()
            assert snapshot["events"][0]["name"] == "blocked_node"
            assert snapshot["events"][0]["status"] == "running"
            assert not pending.done()
            with TestClient(voice_api.app, base_url="http://127.0.0.1") as other:
                assert other.get("/api/trace").status_code == 401
                other.get("/")
                assert other.get("/api/trace").json()["events"] == []
        finally:
            release.set()
        result = pending.result(timeout=3)
    assert result.status_code == 200
    trace = voice_api.get("/api/trace").json()
    assert trace["events"][0]["status"] == "complete"
    assert trace["session_id"] == result.json()["session_id"]
    api_post(voice_api, "start", key="d" * 32)
    assert voice_api.get("/api/trace").json() == trace


def test_trace_reports_graph_failure_without_exception_text(voice_api, monkeypatch):
    from langgraph.graph import END, START, StateGraph

    import collections_lab.channels.voice_session as module

    def failed_node(state):
        raise RuntimeError("private-provider-secret")

    builder = StateGraph(dict)
    builder.add_node("failed_node", failed_node)
    builder.add_edge(START, "failed_node")
    builder.add_edge("failed_node", END)
    monkeypatch.setattr(module, "build_graph", lambda **kwargs: builder.compile())
    assert api_post(voice_api, "start").status_code == 500
    trace = voice_api.get("/api/trace").json()
    assert trace["events"][0]["status"] == "error"
    assert "private-provider-secret" not in json.dumps(trace)


def registry_fixture(tmp_path, monkeypatch):
    from uuid import uuid4

    from collections_lab.channels.voice_session import GraphRegistry

    root = tmp_path / "project"
    source = root / "src"
    source.mkdir(parents=True)
    name = "graph_fixture_" + uuid4().hex
    module = source / f"{name}.py"
    template = """from langgraph.graph import StateGraph, START, END
builder = StateGraph(dict)
builder.add_node("{node}", lambda state: {{"marker": "{node}"}})
builder.add_edge(START, "{node}")
builder.add_edge("{node}", END)
agent = builder.compile()
"""
    module.write_text(template.format(node="alpha"))
    manifest = root / "langgraph.json"
    manifest.write_text(json.dumps({"graphs": {"collections_agent": f"./src/{name}.py:agent"}}))
    monkeypatch.syspath_prepend(str(source))
    return GraphRegistry(root), module, manifest, template


def test_graph_registry_refreshes_between_calls_and_discovers_registered_graphs(
    tmp_path, monkeypatch
):
    registry, module, manifest, template = registry_fixture(tmp_path, monkeypatch)
    first = registry.catalog()
    graph, graph_id, version = registry.acquire()
    assert graph_id == "collections_agent"
    assert graph.invoke({}) == {"marker": "alpha"}
    module.write_text(template.format(node="bravo"))  # Same size and second: avoid stale pyc.
    pending = registry.catalog()
    assert pending["pending_update"] and pending["version"] == version
    assert graph.invoke({}) == {"marker": "alpha"}
    registry.release()
    changed = registry.catalog()
    assert changed["version"] != first["version"]
    assert "bravo" in changed["graphs"][0]["topology"]["nodes"]
    content = json.loads(manifest.read_text())
    content["graphs"]["second_flow"] = content["graphs"]["collections_agent"]
    manifest.write_text(json.dumps(content))
    assert {item["id"] for item in registry.catalog()["graphs"]} == {
        "collections_agent",
        "second_flow",
    }


def test_graph_registry_retains_last_valid_version_on_invalid_edit(tmp_path, monkeypatch):
    registry, module, _manifest, template = registry_fixture(tmp_path, monkeypatch)
    first = registry.catalog()
    module.write_text("private-secret invalid Python!")
    failed = registry.catalog()
    assert failed["error"] and failed["pending_update"]
    assert failed["version"] == first["version"]
    assert "private-secret" not in json.dumps(failed)
    graph, _, _ = registry.acquire()
    assert graph.invoke({}) == {"marker": "alpha"}
    registry.release()
    module.write_text(template.format(node="alpha"))
    assert not registry.catalog()["error"]
    assert registry.catalog()["version"] == first["version"]
    module.write_text(template.format(node="delta"))
    assert not registry.catalog()["error"]
    assert "delta" in registry.catalog()["graphs"][0]["topology"]["nodes"]


def test_automatic_layout_and_trace_handle_parallel_nodes_and_renamed_terminal():
    import operator
    from typing import Annotated, TypedDict

    from langgraph.graph import END, START, StateGraph

    from collections_lab.channels.voice_session import ExecutionTrace

    class ParallelState(TypedDict):
        visited: Annotated[list[str], operator.add]

    builder = StateGraph(ParallelState)
    for name in ("entry", "left", "right", "renamed_terminal"):
        builder.add_node(name, lambda state, name=name: {"visited": [name]})
    builder.add_edge(START, "entry")
    builder.add_edge("entry", "left")
    builder.add_edge("entry", "right")
    builder.add_edge(["left", "right"], "renamed_terminal")
    builder.add_edge("renamed_terminal", END)
    graph = builder.compile()
    trace = ExecutionTrace(graph, "parallel")
    trace.begin()
    for event in graph.stream({"visited": []}, stream_mode="debug"):
        if event["type"] in {"task", "task_result"}:
            trace.task(event["payload"], event["step"])
    trace.complete()
    snapshot = trace.snapshot()
    paths = snapshot["transitions"]
    assert ["left", "right"] not in paths and ["right", "left"] not in paths
    for pair in (
        ["entry", "left"],
        ["entry", "right"],
        ["left", "renamed_terminal"],
        ["right", "renamed_terminal"],
        ["renamed_terminal", "__end__"],
    ):
        assert pair in paths
    topology = snapshot["topology"]
    positions = topology["positions"]
    assert len(set(map(tuple, positions.values()))) == len(topology["nodes"])
    assert all(
        68 < x < topology["width"] - 68 and 23 < y < topology["height"] - 23
        for x, y in positions.values()
    )


def test_automatic_layout_preserves_cycles():
    from langgraph.graph import END, START, StateGraph

    from collections_lab.channels.voice_session import graph_topology

    builder = StateGraph(dict)
    builder.add_node("renamed_loop", lambda state: {})
    builder.add_edge(START, "renamed_loop")
    builder.add_conditional_edges("renamed_loop", lambda state: END, ["renamed_loop", END])
    topology = graph_topology(builder.compile())
    assert any(edge["source"] == edge["target"] == "renamed_loop" for edge in topology["edges"])
    assert set(topology["positions"]) == set(topology["nodes"])


def test_failed_start_preserves_new_call_for_audio_retry(voice_api, session, monkeypatch):
    state = api_post(voice_api, "start").json()
    api_post(voice_api, "end", state=state)
    synthesize = session.speech.synthesize

    def fail(text):
        raise RuntimeError("private-provider-secret")

    monkeypatch.setattr(session.speech, "synthesize", fail)
    response = api_post(voice_api, "start")
    assert response.status_code == 500
    current = voice_api.get("/api/session").json()
    assert current["session_id"] != state["session_id"]
    assert current["session_id"] == voice_api.get("/api/trace").json()["session_id"]
    assert len(current["history"]) == 2 and not current["finished"]
    assert "private-provider-secret" not in response.text
    monkeypatch.setattr(session.speech, "synthesize", synthesize)
    replay = api_post(voice_api, "retry", state=current).json()
    assert replay["audio_url"] and replay["history"] == current["history"]


def test_session_and_audio_reads_return_immediately_while_synthesis_runs(
    voice_api, session, monkeypatch
):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    state = finish_playback(voice_api, api_post(voice_api, "start").json())
    entered, release = Event(), Event()

    def blocked(text):
        entered.set()
        assert release.wait(5)
        return b"ID3-demo"

    monkeypatch.setattr(session.speech, "synthesize", blocked)
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = pool.submit(
            api_post, voice_api, "text", state=state, payload={"text": "123456789"}
        )
        try:
            assert entered.wait(3)
            current = pool.submit(voice_api.get, "/api/session").result(timeout=2).json()
            assert current["busy"] and not current["ready"]
            assert current["history"] == state["history"]
            audio = pool.submit(voice_api.get, state["audio_url"]).result(timeout=2)
            assert audio.status_code == 200 and not pending.done()
        finally:
            release.set()
        assert pending.result(timeout=3).status_code == 200
    assert not voice_api.get("/api/session").json()["busy"]


def test_deep_validation_error_is_redacted_and_failed_call_cannot_repeat_graph(
    voice_api, monkeypatch
):
    from langgraph.graph import END, START, StateGraph

    import collections_lab.channels.voice_session as module

    calls = []

    def failed_node(state):
        calls.append(True)
        raise ValueError("private-graph-secret")

    builder = StateGraph(dict)
    builder.add_node("failed_node", failed_node)
    builder.add_edge(START, "failed_node")
    builder.add_edge("failed_node", END)
    monkeypatch.setattr(module, "build_graph", lambda **kwargs: builder.compile())
    result = api_post(voice_api, "start")
    assert result.status_code == 500 and "private-graph-secret" not in result.text
    current = voice_api.get("/api/session").json()
    assert current["failed"] and not current["ready"]
    assert api_post(voice_api, "text", state=current, payload={"text": "retry"}).status_code == 400
    assert len(calls) == 1
    assert api_post(voice_api, "end", state=current).json()["finished"]


def test_graph_failure_rolls_back_agreement_and_blocks_repeated_turn(session, monkeypatch):
    offer(session)
    session.acknowledge_playback(session.audio)
    stream = session.graph.stream

    def interrupted(*args, **kwargs):
        for mode, chunk in stream(*args, **kwargs):
            yield mode, chunk
            if (
                mode == "debug"
                and chunk["type"] == "task_result"
                and chunk["payload"]["name"] == "response"
            ):
                raise RuntimeError("after committed response")

    monkeypatch.setattr(session.graph, "stream", interrupted)
    with pytest.raises(RuntimeError):
        session.send("confirmo")
    assert not session.context.repository.agreements()
    with pytest.raises(ValueError, match="Finaliza"):
        session.send("confirmo")
    assert not session.context.repository.agreements()


@pytest.mark.parametrize("failure", ["publish", "snapshot"])
def test_snapshot_failure_clears_busy_and_caches_uncertain_outcome(
    voice_api, session, monkeypatch, failure
):
    from uuid import uuid4

    state = finish_playback(voice_api, api_post(voice_api, "start").json())
    original = VoiceSession.details
    calls = 0

    def broken_details(self):
        nonlocal calls
        calls += 1
        if (failure == "publish" and calls == 3) or (failure == "snapshot" and calls >= 2):
            raise RuntimeError("private-publication-error")
        return original(self)

    key = uuid4().hex
    with monkeypatch.context() as patch:
        patch.setattr(VoiceSession, "details", broken_details)
        response = api_post(voice_api, "text", state=state, payload={"text": "123456789"}, key=key)
        assert response.status_code == 500
        assert "private-publication-error" not in response.text
        count = len(session.speech.calls)
        assert (
            api_post(voice_api, "text", state=state, payload={"text": "123456789"}, key=key).json()
            == response.json()
        )
        assert len(session.speech.calls) == count
        current = voice_api.get("/api/session").json()
        assert not current["busy"] and current["failed"] and not current["ready"]
    assert api_post(voice_api, "end", state=current).json()["finished"]


@pytest.mark.parametrize("output", [None, {}, {"reply": {"text": "missing stage"}}])
def test_invalid_stream_reply_rolls_back_and_quarantines_the_call(session, monkeypatch, output):
    offer(session)
    session.acknowledge_playback(session.audio)
    original = session.graph.stream
    history = list(session.history)
    speech_calls = len(session.speech.calls)

    def invalid_stream(*args, **kwargs):
        for mode, chunk in original(*args, **kwargs):
            if mode == "debug":
                yield mode, chunk
        if output is not None:
            yield "values", output

    monkeypatch.setattr(session.graph, "stream", invalid_stream)
    with pytest.raises(TypeError, match="invalid voice reply"):
        session.send("confirmo")
    assert session.failed and not session.trace.snapshot()["graph_complete"]
    assert not session.context.repository.agreements()
    assert session.history == history and len(session.speech.calls) == speech_calls
    with pytest.raises(ValueError, match="Finaliza"):
        session.send("confirmo")


def test_trace_eviction_does_not_invent_a_new_start_step(session):
    trace = session.trace
    trace.begin()
    entry = next(
        edge["target"] for edge in trace.topology["edges"] if edge["source"] == "__start__"
    )
    for step in range(201):
        event_id = trace.start(entry, step=step)
        trace.finish(event_id)
    result = trace.snapshot()
    assert len(result["events"]) == 200
    assert ["__start__", entry] not in result["transitions"]
    trace.begin()
    trace.start(entry, step=300)
    assert ["__start__", entry] in trace.snapshot()["transitions"]


def test_idle_trace_uses_refreshed_catalog(session, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from collections_lab.channels.voice_app import create_app

    registry, module, _, template = registry_fixture(tmp_path, monkeypatch)
    app = create_app(
        session.speech, data_dir=tmp_path / "api", now=session.context.now, graph_registry=registry
    )
    with TestClient(app, base_url="http://127.0.0.1") as client:
        client.get("/")
        first = client.get("/api/trace").json()
        assert "alpha" in first["topology"]["nodes"]
        module.write_text(template.format(node="bravo"))
        second = client.get("/api/trace").json()
        assert "bravo" in second["topology"]["nodes"]
        assert second["graph_version"] != first["graph_version"]
        assert not second["events"] and not second["session_id"]


def test_failed_initial_publication_keeps_call_reachable_for_hangup(voice_api, monkeypatch):
    def broken_details(self):
        raise RuntimeError("publication failure")

    with monkeypatch.context() as patch:
        patch.setattr(VoiceSession, "details", broken_details)
        response = api_post(voice_api, "start")
        assert response.status_code == 500
        current = voice_api.get("/api/session").json()
        assert current["session_id"] and current["failed"] and not current["busy"]
    assert api_post(voice_api, "end", state=current).json()["finished"]


def test_trace_completion_failure_rolls_back_agreement(session, monkeypatch):
    offer(session)
    session.acknowledge_playback(session.audio)

    def fail():
        raise RuntimeError("trace completion failed")

    monkeypatch.setattr(session.trace, "complete", fail)
    with pytest.raises(RuntimeError, match="trace completion"):
        session.send("confirmo")
    assert session.failed and not session.trace.snapshot()["graph_complete"]
    assert not session.context.repository.agreements()
    with pytest.raises(ValueError, match="Finaliza"):
        session.send("confirmo")


def test_hangup_remains_finished_during_permanent_snapshot_failure(voice_api, monkeypatch):
    state = api_post(voice_api, "start").json()
    released = []
    release = VoiceSession.release_graph

    def record_release(self):
        released.append(self.thread_id)
        release(self)

    def fail(self):
        raise RuntimeError("persistent snapshot failure")

    monkeypatch.setattr(VoiceSession, "details", fail)
    monkeypatch.setattr(VoiceSession, "release_graph", record_release)
    result = api_post(voice_api, "end", state=state)
    assert result.status_code == 500
    current = voice_api.get("/api/session").json()
    assert current["finished"] and not current["busy"] and not current["ready"]
    assert state["session_id"] in released


def test_unheard_offer_cannot_use_text_as_a_playback_bypass(session, monkeypatch):
    from agents.collections import semantic

    offer(session)
    calls = []

    def accept(*args, **kwargs):
        calls.append(True)
        return semantic.TurnResult(
            semantic.TurnUnderstanding(
                intent="accept_offer", acceptance="unconditional_current_offer"
            ),
            "classified",
        )

    monkeypatch.setattr(semantic, "understand_turn", accept)
    with pytest.raises(ValueError, match="Espera"):
        session.send("terminar")
    assert not calls and not session.context.repository.agreements()
    assert session.end_call()["finished"]
    assert not calls


def test_hangup_does_not_call_semantic_or_speech_services(voice_api, session, monkeypatch):
    from agents.collections import semantic

    state = api_post(voice_api, "start").json()
    speech_calls = len(session.speech.calls)

    def unavailable(*args, **kwargs):
        raise AssertionError("Hangup must not call a model")

    monkeypatch.setattr(semantic, "understand_turn", unavailable)
    response = api_post(voice_api, "end", state=state)
    assert response.status_code == 200 and response.json()["finished"]
    assert not response.json()["audio_url"]
    assert len(session.speech.calls) == speech_calls


@pytest.mark.parametrize("suffix", [".jinja", ".jinja2"])
def test_registry_reloads_cached_jinja_templates_between_calls(tmp_path, monkeypatch, suffix):
    registry, module, _, _ = registry_fixture(tmp_path, monkeypatch)
    template = module.with_suffix(suffix)
    template.write_text("first prompt")
    module.write_text(f"""from pathlib import Path
from jinja2 import Template
from langgraph.graph import StateGraph, START, END
prompt = Template(Path(__file__).with_suffix({suffix!r}).read_text())
builder = StateGraph(dict)
builder.add_node("answer", lambda state: {{"prompt": prompt.render()}})
builder.add_edge(START, "answer")
builder.add_edge("answer", END)
agent = builder.compile()
""")
    graph, _, version = registry.acquire()
    assert graph.invoke({})["prompt"] == "first prompt"
    template.write_text("new prompt")
    assert registry.catalog()["pending_update"]
    assert graph.invoke({})["prompt"] == "first prompt"
    registry.release()
    current, _, new_version = registry.acquire()
    assert current.invoke({})["prompt"] == "new prompt" and new_version != version
    registry.release()
    template.write_text("{% invalid %}")
    catalog = registry.catalog()
    assert catalog["error"] and catalog["version"] == new_version
    retained, _, _ = registry.acquire()
    assert retained.invoke({})["prompt"] == "new prompt"
    registry.release()
    template.write_text("new prompt")
    assert not registry.catalog()["error"]


def test_registered_repository_graph_uses_one_semantic_pass_per_voice_turn(tmp_path):
    import subprocess
    import sys
    from pathlib import Path

    script = """
import socket
import sys
from datetime import datetime
from pathlib import Path

def no_network(*args, **kwargs):
    raise AssertionError("Offline registry integration must not call providers")
socket.socket.connect = no_network
from agents.collections.memory import SQLiteRepository
from agents.collections.runtime import AppContext
from collections_lab.channels.voice_session import GraphRegistry, VoiceSession

class Speech:
    def synthesize(self, text):
        return b"ID3-offline"

root = Path.cwd()
data = Path(sys.argv[1])
registry = GraphRegistry(root)
context = AppContext(SQLiteRepository(data / "conversation.db"), datetime.fromisoformat("2026-10-19T09:00:00-05:00"))
call = VoiceSession(context, Speech(), data / "audio", graph_registry=registry)
from agents.collections import semantic
original = semantic.understand_turn
calls = []
def understand(*args, **kwargs):
    calls.append((args[0], kwargs["stage"]))
    return original(*args, **kwargs)
semantic.understand_turn = understand
stages = []
for text in ["Hola", "123456789", "1111", "quiero 3 cuotas desde 2026-10-20"]:
    stages.append(call.send(text)["stage"])
    nodes = [event["name"] for event in call.trace.snapshot()["events"]]
    assert nodes.count("understand_message") == 1
    assert nodes.count("route_chat_message") == 1
assert stages == ["awaiting_customer", "awaiting_identity", "ready", "awaiting_confirmation"]
assert len(calls) == 4
call.retry_audio()
assert len(calls) == 4
call.end_call()
assert len(calls) == 4 and registry._leases == 0
assert not call.context.repository.agreements()
print("registered graph + semantic turns + playback retry + hangup: OK")
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "hangup: OK" in result.stdout


def test_offer_id_enforces_playback_even_after_reply_stage_changes(voice_api, session, monkeypatch):
    retry_audio = VoiceSession.retry_audio

    def changed_reply(self):
        if self.reply.get("offer_id"):
            self.reply["stage"] = "awaiting_terms"
        return retry_audio(self)

    monkeypatch.setattr(VoiceSession, "retry_audio", changed_reply)
    state = api_post(voice_api, "start").json()
    for text in ["123456789", "1111"]:
        state = finish_playback(voice_api, state)
        state = api_post(voice_api, "text", state=state, payload={"text": text}).json()
    state = finish_playback(voice_api, state)
    session.speech.fail = True
    state = api_post(
        voice_api, "text", state=state, payload={"text": "quiero 3 cuotas desde 2026-10-20"}
    ).json()
    assert state["details"]["stage"] == "awaiting_terms" and state["details"]["offer_id"]
    assert not state["ready"] and not state["audio_id"]
    assert api_post(voice_api, "text", state=state, payload={"text": "confirmo"}).status_code == 400
    assert api_post(voice_api, "recording", state=state, content=b"demo").status_code == 400
    assert not session.speech.transcriptions
    session.speech.fail = False
    state = api_post(voice_api, "retry", state=state).json()
    assert state["audio_id"] and not state["ready"]
    assert finish_playback(voice_api, state)["ready"]


def test_session_offer_id_blocks_direct_submissions_after_stage_change(session):
    offer(session)
    session.reply["stage"] = "awaiting_terms"
    with pytest.raises(ValueError, match="Espera"):
        session.send("confirmo")
    session.acknowledge_playback(session.audio)
    session.check_turn()
