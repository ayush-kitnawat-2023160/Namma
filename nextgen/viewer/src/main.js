import * as GaussianSplats3D from '@mkkellogg/gaussian-splats-3d';
import './style.css';

const params = new URLSearchParams(window.location.search);
const scene = params.get('scene') || 'scene';
const iteration = params.get('iter') || '10000';
const alphaThreshold = Number(params.get('alpha') || '5');

const plyUrl = `/scene/06_gs_model/point_cloud/iteration_${iteration}/point_cloud.ply`;
const rendersUrl = '/scene/07_renders/renders/';

const $ = (id) => document.getElementById(id);
const viewerRoot = $('viewer');
const welcome = $('welcome');
const loading = $('loading');
const status = $('status');
const loadButton = $('load-button');
const resetButton = $('reset-button');
const rendersPanel = $('renders-panel');
const renderGrid = $('render-grid');
const renderEmpty = $('render-empty');

$('scene-title').textContent = scene;
$('welcome-title').textContent = scene;
$('iteration-value').textContent = iteration;
document.title = `${scene} · Namma NextGen`;

let viewer = null;
let renderNames = [];

function bytes(value) {
  if (!Number.isFinite(value) || value <= 0) return 'unknown';
  const units = ['B', 'KB', 'MB', 'GB'];
  let amount = value;
  let unit = 0;
  while (amount >= 1024 && unit < units.length - 1) {
    amount /= 1024;
    unit += 1;
  }
  return `${amount.toFixed(unit >= 2 ? 1 : 0)} ${units[unit]}`;
}

async function probePly() {
  try {
    const response = await fetch(plyUrl, { method: 'HEAD' });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const length = Number(response.headers.get('content-length'));
    $('ply-size').textContent = bytes(length);
    return true;
  } catch (error) {
    $('ply-size').textContent = 'missing';
    loadButton.disabled = true;
    loadButton.textContent = 'Gaussian PLY not found';
    status.textContent = `PLY unavailable: ${error.message}`;
    return false;
  }
}

async function discoverRenders() {
  try {
    const response = await fetch(rendersUrl);
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const html = await response.text();
    const doc = new DOMParser().parseFromString(html, 'text/html');
    renderNames = [...doc.querySelectorAll('a')]
      .map((a) => a.getAttribute('href'))
      .filter((href) => href && /\.(png|jpe?g|webp)$/i.test(href))
      .map((href) => decodeURIComponent(href.split('/').pop()))
      .filter(Boolean);
    $('render-count').textContent = String(renderNames.length);
  } catch {
    renderNames = [];
    $('render-count').textContent = '0';
  }
}

async function loadScene() {
  if (viewer) return;
  loadButton.disabled = true;
  welcome.classList.add('hidden');
  loading.classList.remove('hidden');
  status.textContent = 'Loading Gaussian PLY…';

  try {
    viewer = new GaussianSplats3D.Viewer({
      rootElement: viewerRoot,
      cameraUp: [0, -1, -0.6],
      initialCameraPosition: [0, -3, 1.5],
      initialCameraLookAt: [0, 0, 0],
      sharedMemoryForWorkers: false,
      gpuAcceleratedSort: true,
      dynamicScene: false,
      enableSIMDInSort: true,
      integerBasedSort: false,
      antialiased: true,
    });

    await viewer.addSplatScene(plyUrl, {
      splatAlphaRemovalThreshold: alphaThreshold,
      showLoadingUI: true,
      progressiveLoad: true,
    });
    viewer.start();
    loading.classList.add('hidden');
    resetButton.disabled = false;
    status.textContent = `Interactive scene loaded · iteration ${iteration}`;
  } catch (error) {
    console.error(error);
    loading.classList.add('hidden');
    welcome.classList.remove('hidden');
    loadButton.disabled = false;
    loadButton.textContent = 'Retry interactive 3D';
    status.textContent = `Viewer error: ${error.message}`;
  }
}

function resetView() {
  // The built-in viewer owns its orbit controls. Recreating it gives us a
  // deterministic reset without reaching into private control internals.
  if (!viewer) return;
  window.location.reload();
}

function buildRenderGrid() {
  renderGrid.replaceChildren();
  renderEmpty.classList.toggle('hidden', renderNames.length > 0);
  for (const name of renderNames) {
    const link = document.createElement('a');
    link.className = 'render-card';
    link.href = `${rendersUrl}${encodeURIComponent(name)}`;
    link.target = '_blank';
    link.rel = 'noreferrer';

    const image = document.createElement('img');
    image.loading = 'lazy';
    image.alt = name;
    image.src = link.href;

    const label = document.createElement('span');
    label.textContent = name;
    link.append(image, label);
    renderGrid.appendChild(link);
  }
}

function openRenders() {
  buildRenderGrid();
  rendersPanel.classList.remove('hidden');
}

loadButton.addEventListener('click', loadScene);
resetButton.addEventListener('click', resetView);
$('renders-button').addEventListener('click', openRenders);
$('open-renders-button').addEventListener('click', openRenders);
$('close-renders').addEventListener('click', () => rendersPanel.classList.add('hidden'));

Promise.all([probePly(), discoverRenders()]).then(() => {
  status.textContent = `Ready · ${scene}`;
});
