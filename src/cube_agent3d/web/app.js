import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";

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

// --- Three.js
const renderer = new THREE.WebGLRenderer({ canvas: elCanvas, antialias: true });
renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));

const scene = new THREE.Scene();
scene.background = new THREE.Color(0x0b0f17);

const camera = new THREE.PerspectiveCamera(60, 1, 0.05, 500);
camera.position.set(8, 8, 10);

const controls = new OrbitControls(camera, renderer.domElement);
controls.enableDamping = true;
controls.dampingFactor = 0.08;
controls.minDistance = 3;
controls.maxDistance = 60;
controls.target.set(0, 4, 0);

elZoomReset.addEventListener("click", () => {
  camera.position.set(8, 8, 10);
  controls.target.set(0, 4, 0);
  controls.update();
});

scene.add(new THREE.AmbientLight(0xffffff, 0.55));
const dir = new THREE.DirectionalLight(0xffffff, 1.0);
dir.position.set(10, 20, 10);
scene.add(dir);

const grid = new THREE.GridHelper(40, 40, 0x223044, 0x121a27);
grid.position.y = 0;
scene.add(grid);

// Instanced cubes
const MAX_INST = 256;
const geom = new THREE.BoxGeometry(1, 1, 1);
const mat = new THREE.MeshStandardMaterial({ vertexColors: true, roughness: 0.65, metalness: 0.1 });
const inst = new THREE.InstancedMesh(geom, mat, MAX_INST);
inst.instanceMatrix.setUsage(THREE.DynamicDrawUsage);
scene.add(inst);

const _m = new THREE.Matrix4();
const _p = new THREE.Vector3();
const _q = new THREE.Quaternion();
const _s = new THREE.Vector3();

function applySnapshot(payload) {
  const cubes = payload.cubes || [];
  const count = Math.min(cubes.length, MAX_INST);

  for (let i = 0; i < count; i++) {
    const c = cubes[i];

    _p.set(c.pos[0], c.pos[1], c.pos[2]);
    _q.set(c.rot[0], c.rot[1], c.rot[2], c.rot[3]);
    _s.set(c.scale[0], c.scale[1], c.scale[2]);

    _m.compose(_p, _q, _s);
    inst.setMatrixAt(i, _m);
    inst.setColorAt(i, new THREE.Color(c.color || "#7dd3fc"));
  }

  for (let i = count; i < MAX_INST; i++) {
    _m.identity();
    _m.makeScale(0, 0, 0);
    inst.setMatrixAt(i, _m);
  }

  inst.instanceMatrix.needsUpdate = true;
  if (inst.instanceColor) inst.instanceColor.needsUpdate = true;
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
    let msg;
    try { msg = JSON.parse(ev.data); } catch { return; }

    const type = msg.type;
    const payload = msg.payload || {};

    if (type === "SERVER_STATUS") {
      const run = payload.running ? "RUN" : "STOP";
      elStatus.innerText = `${run} | tick=${payload.tick} | cubes=${payload.cube_count}`;
      elMeta.innerText = `session=${payload.session_id} | tick_hz=${payload.tick_hz} | max_cubes=${payload.max_cubes}`;
    } else if (type === "STATE_SNAPSHOT") {
      applySnapshot(payload);
    } else if (type === "ACTION_BATCH") {
      const score = payload.score || 0;
      const actions = payload.actions || [];
      logLine(`tick=${payload.tick} score=${score.toFixed(2)} actions=${actions.length}`);
    }
  };
}

elStart.addEventListener("click", () => {
  if (ws?.readyState === 1) ws.send(JSON.stringify({ type: "UI_START", payload: {} }));
});
elStop.addEventListener("click", () => {
  if (ws?.readyState === 1) ws.send(JSON.stringify({ type: "UI_STOP", payload: {} }));
});
elReset.addEventListener("click", () => {
  if (ws?.readyState === 1) ws.send(JSON.stringify({ type: "RESET", payload: {} }));
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
