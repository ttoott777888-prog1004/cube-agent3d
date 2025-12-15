import * as THREE from "https://unpkg.com/three@0.160.0/build/three.module.js";
import { OrbitControls } from "https://unpkg.com/three@0.160.0/examples/jsm/controls/OrbitControls.js";

const elCanvas = document.getElementById("canvas");
const elStart = document.getElementById("btnStart");
const elStop = document.getElementById("btnStop");
const elReset = document.getElementById("btnReset");
const elZoomReset = document.getElementById("btnZoomReset");
const elStatus = document.getElementById("status");
const elMeta = document.getElementById("meta");
const elLog = document.getElementById("log");

function logLine(s) {
  const t = new Date().toLocaleTimeString();
  elLog.innerText = `[${t}] ${s}\n` + elLog.innerText;
}

let ws = null;

// --- Three.js scene
const renderer = new THREE.WebGLRenderer({ canvas: elCanvas, antialias: true });
renderer.setPixelRatio(window.devicePixelRatio || 1);

const scene = new THREE.Scene();
scene.background = new THREE.Color(0x0b0f17);

const camera = new THREE.PerspectiveCamera(60, 1, 0.05, 800);
camera.position.set(10, 10, 14);

const controls = new OrbitControls(camera, renderer.domElement);
controls.enableDamping = true;
controls.dampingFactor = 0.08;
controls.minDistance = 3;
controls.maxDistance = 160;
controls.target.set(0, 4, 0);

elZoomReset.addEventListener("click", () => {
  camera.position.set(10, 10, 14);
  controls.target.set(0, 4, 0);
  controls.update();
});

scene.add(new THREE.AmbientLight(0xffffff, 0.55));
const dir = new THREE.DirectionalLight(0xffffff, 1.0);
dir.position.set(10, 20, 10);
scene.add(dir);

const grid = new THREE.GridHelper(80, 80, 0x223044, 0x121a27);
grid.position.y = 0;
scene.add(grid);

// ====== Instanced cubes (최대 2048 렌더)
const MAX_CUBES_RENDER = 2048;
const cubeGeom = new THREE.BoxGeometry(1, 1, 1);
const cubeMat = new THREE.MeshStandardMaterial({
  vertexColors: true,
  roughness: 0.65,
  metalness: 0.1,
});
const cubesInst = new THREE.InstancedMesh(cubeGeom, cubeMat, MAX_CUBES_RENDER);
cubesInst.instanceMatrix.setUsage(THREE.DynamicDrawUsage);
scene.add(cubesInst);

// ====== Probes (초록 반투명 박스)
const MAX_PROBES_RENDER = 128;
const probeGeom = new THREE.BoxGeometry(1, 1, 1);
const probeMat = new THREE.MeshBasicMaterial({
  color: 0x00ff66,
  transparent: true,
  opacity: 0.20,
  depthWrite: false,
});
const probesInst = new THREE.InstancedMesh(probeGeom, probeMat, MAX_PROBES_RENDER);
probesInst.instanceMatrix.setUsage(THREE.DynamicDrawUsage);
scene.add(probesInst);

const _m = new THREE.Matrix4();
const _p = new THREE.Vector3();
const _q = new THREE.Quaternion();
const _s = new THREE.Vector3();
const _col = new THREE.Color();

function applySnapshot(payload) {
  const cubes = payload.cubes || [];
  const probes = payload.probes || [];

  // 상태 텍스트(에피소드/상한 표시)
  const tick = payload.tick ?? 0;
  const episode = payload.episode ?? "-";
  const cap = payload.episode_cap ?? "-";
  const capMax = payload.episode_cap_max ?? "-";
  elMeta.innerText = `episode=${episode} | cap=${cap}/${capMax} | tick=${tick}`;

  // ---- cubes
  const nC = Math.min(cubes.length, MAX_CUBES_RENDER);
  cubesInst.count = nC;

  for (let i = 0; i < nC; i++) {
    const c = cubes[i];
    const pos = c.pos || [0, 0, 0];
    const rot = c.rot || [0, 0, 0, 1];
    const scl = c.scale || [1, 1, 1];

    _p.set(pos[0], pos[1], pos[2]);
    _q.set(rot[0], rot[1], rot[2], rot[3]);
    _s.set(scl[0], scl[1], scl[2]);
    _m.compose(_p, _q, _s);
    cubesInst.setMatrixAt(i, _m);

    _col.set(c.color || "#222222");
    cubesInst.setColorAt(i, _col);
  }
  cubesInst.instanceMatrix.needsUpdate = true;
  if (cubesInst.instanceColor) cubesInst.instanceColor.needsUpdate = true;

  // ---- probes
  const nP = Math.min(probes.length, MAX_PROBES_RENDER);
  probesInst.count = nP;

  for (let i = 0; i < nP; i++) {
    const p = probes[i];
    const pos = p.pos || [0, 0, 0];
    const scl = p.scale || [1.02, 1.02, 1.02];

    _p.set(pos[0], pos[1], pos[2]);
    _q.set(0, 0, 0, 1);
    _s.set(scl[0], scl[1], scl[2]);
    _m.compose(_p, _q, _s);
    probesInst.setMatrixAt(i, _m);
  }
  probesInst.instanceMatrix.needsUpdate = true;
}

function connectWS() {
  const proto = (location.protocol === "https:") ? "wss" : "ws";
  const url = `${proto}://${location.host}/ws`;
  ws = new WebSocket(url);

  ws.onopen = () => {
    elStatus.innerText = "연결됨";
    ws.send(JSON.stringify({ type: "HELLO", payload: { client: "web" } }));
    logLine("WebSocket 연결됨");
  };

  ws.onclose = () => {
    elStatus.innerText = "연결 끊김";
    logLine("WebSocket 연결 끊김");
    setTimeout(connectWS, 800);
  };

  ws.onerror = () => {
    elStatus.innerText = "오류";
  };

  ws.onmessage = (ev) => {
    let msg = null;
    try { msg = JSON.parse(ev.data); } catch { return; }

    const type = msg.type;
    const payload = msg.payload || {};

    if (type === "SERVER_STATUS") {
      const run = payload.running ? "RUN" : "STOP";
      elStatus.innerText = `${run} | tick=${payload.tick} | cubes=${payload.cube_count}`;
      // meta는 snapshot에서 episode/cap를 더 자세히 보여주므로 여기서는 최소만
    }

    if (type === "STATE_SNAPSHOT") {
      applySnapshot(payload);
    }

    if (type === "ACTION_BATCH") {
      const score = payload.score ?? 0;
      const actions = payload.actions || [];
      if (actions.length > 0) {
        const head = actions[0];
        logLine(`tick=${payload.tick} score=${Number(score).toFixed(2)} actions=${actions.length} head=${head.type}`);
      } else {
        logLine(`tick=${payload.tick} score=${Number(score).toFixed(2)} actions=0`);
      }
    }
  };
}

elStart.addEventListener("click", () => {
  if (!ws || ws.readyState !== 1) return;
  ws.send(JSON.stringify({ type: "UI_START", payload: {} }));
});

elStop.addEventListener("click", () => {
  if (!ws || ws.readyState !== 1) return;
  ws.send(JSON.stringify({ type: "UI_STOP", payload: {} }));
});

elReset.addEventListener("click", () => {
  if (!ws || ws.readyState !== 1) return;
  ws.send(JSON.stringify({ type: "RESET", payload: {} }));
  logLine("RESET 요청");
});

function resize() {
  const w = window.innerWidth;
  const h = window.innerHeight;
  renderer.setSize(w, h, false);
  camera.aspect = w / h;
  camera.updateProjectionMatrix();
}
window.addEventListener("resize", resize);
resize();

function animate() {
  requestAnimationFrame(animate);
  controls.update();
  renderer.render(scene, camera);
}
animate();

connectWS();
