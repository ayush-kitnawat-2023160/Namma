import * as GaussianSplats3D from '@mkkellogg/gaussian-splats-3d';
import './style.css';

const params = new URLSearchParams(window.location.search);
const scene = params.get('scene') || 'scene';
const iteration = params.get('iter') || '10000';
const supportedThresholds = [1, 5, 10];
const requestedThreshold = Number(params.get('alpha') || '5');
let alphaThreshold = supportedThresholds.includes(requestedThreshold) ? requestedThreshold : 5;
const requestedShDegree = Number(params.get('sh') || '2');
const shDegree = [0, 1, 2].includes(requestedShDegree) ? requestedShDegree : 2;
const requestedModel = params.get('model');
const modelVariant = ['cleaned', 'depth-aware'].includes(requestedModel) ? requestedModel : 'trained';

const modelPaths = {
  cleaned: '/scene/08_postprocess/cleaned.ply',
  'depth-aware': '/scene/08_postprocess/depth_aware_cleaned.ply',
};
const basePlyUrl = modelPaths[modelVariant]
  || `/scene/06_gs_model/point_cloud/iteration_${iteration}/point_cloud.ply`;
let plyUrl = basePlyUrl;
const rendersUrl = '/scene/07_renders/renders/';

const $ = (id) => document.getElementById(id);
const viewerRoot = $('viewer');
const welcome = $('welcome');
const loading = $('loading');
const loadingTitle = $('loading-title');
const loadingDetail = $('loading-detail');
const loadingProgress = $('loading-progress-value');
const loadingProgressTrack = document.querySelector('.loading-progress');
const status = $('status');
const loadButton = $('load-button');
const resetButton = $('reset-button');
const shControl = $('sh-degree');
const alphaControl = $('alpha-threshold');
const rendersPanel = $('renders-panel');
const renderGrid = $('render-grid');
const renderEmpty = $('render-empty');

$('scene-title').textContent = scene;
$('welcome-title').textContent = scene;
$('iteration-value').textContent = iteration;
document.title = `${scene} · Namma NextGen`;
shControl.value = String(shDegree);
alphaControl.value = String(alphaThreshold);

let viewer = null;
let renderNames = [];
let sceneLoading = false;
let progressivePreviewReady = false;

function updateLoadProgress(percent, label, loaderStatus) {
  const safePercent = Math.max(0, Math.min(100, Number(percent) || 0));
  const displayedPercent = Math.floor(safePercent);
  loadingProgress.style.width = `${safePercent}%`;
  loadingProgressTrack.setAttribute('aria-valuenow', String(displayedPercent));

  if (loaderStatus === 1) {
    loadingTitle.textContent = 'Finishing scene preparation…';
    loadingDetail.textContent = 'All scene data has arrived. Preparing the final render buffers.';
    status.textContent = 'Scene data downloaded · preparing render buffers';
    return;
  }

  if (loaderStatus === 2) {
    loadingTitle.textContent = 'Finalizing scene…';
    loadingDetail.textContent = 'The last splats are being added to the interactive view.';
    return;
  }

  loadingTitle.textContent = progressivePreviewReady
    ? 'Preview is rendering while the full scene loads…'
    : 'Loading Gaussian scene…';
  loadingDetail.textContent = `Downloading scene · ${label || `${displayedPercent}%`} of ${$('ply-size').textContent}. ${progressivePreviewReady ? 'The preview is building as data arrives.' : 'This large scene may take a few minutes on first load.'}`;
  status.textContent = progressivePreviewReady
    ? `Preview rendering · ${displayedPercent}% loaded`
    : `Loading scene · ${displayedPercent}%`;
}

async function waitForProgressiveSceneBuild() {
  // GaussianSplats3D resolves addSplatScene after its first progressive
  // section. Its completion promise covers the remaining download/build work.
  const completion = viewer?.splatSceneDownloadAndBuildPromise;
  if (completion) await completion;
}

function syncViewerSettingsUrl() {
  const updated = new URL(window.location.href);
  updated.searchParams.set('sh', shControl.value);
  updated.searchParams.set('alpha', String(alphaThreshold));
  window.history.replaceState({}, '', updated);
}

function setControlsDisabled(disabled) {
  shControl.disabled = disabled;
  alphaControl.disabled = disabled;
}

async function reloadOpacityCutoff() {
  if (!viewer || sceneLoading) return;
  sceneLoading = true;
  alphaThreshold = Number(alphaControl.value);
  setControlsDisabled(true);
  loading.classList.remove('hidden');
  loadingTitle.textContent = 'Applying opacity cutoff…';
  loadingDetail.textContent = 'Updating the loaded splats in memory. The PLY will not be downloaded again.';
  loadingProgressTrack.classList.add('indeterminate');
  loadingProgress.style.width = '35%';
  loadingProgressTrack.setAttribute('aria-valuenow', '0');
  status.textContent = `Applying ${alphaThreshold}/255 opacity cutoff from memory…`;
  syncViewerSettingsUrl();
  try {
    const loadedScene = viewer.splatMesh.scenes[0];
    if (!loadedScene?.splatBuffer) throw new Error('The Gaussian scene is not loaded yet.');

    await viewer.addSplatBuffers(
      [loadedScene.splatBuffer],
      [{ splatAlphaRemovalThreshold: alphaThreshold }],
      true,
      true,
      true,
      true,
    );
    status.textContent = `Interactive ${modelVariant} scene loaded · opacity ${alphaThreshold}/255`;
  } catch (error) {
    console.error(error);
    status.textContent = `Could not reload splats: ${error.message}`;
  } finally {
    loadingProgressTrack.classList.remove('indeterminate');
    loading.classList.add('hidden');
    setControlsDisabled(false);
    sceneLoading = false;
  }
}

shControl.addEventListener('change', () => {
  const value = Number(shControl.value);
  if (viewer && [0, 1, 2].includes(value)) {
    viewer.setActiveSphericalHarmonicsDegrees(value);
    status.textContent = `Spherical harmonics detail: ${['low', 'medium', 'high'][value]}`;
  }
  syncViewerSettingsUrl();
});

alphaControl.addEventListener('change', reloadOpacityCutoff);

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
    const response = await fetch(basePlyUrl, { method: 'HEAD', cache: 'no-cache' });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const length = Number(response.headers.get('content-length'));
    const version = response.headers.get('etag')
      || `${response.headers.get('last-modified') || 'unknown'}-${length}`;
    const versionedUrl = new URL(basePlyUrl, window.location.origin);
    versionedUrl.searchParams.set('v', version.replaceAll('"', ''));
    plyUrl = versionedUrl.toString();
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
  sceneLoading = true;
  setControlsDisabled(true);
  loadButton.disabled = true;
  welcome.classList.add('hidden');
  loading.classList.remove('hidden');
  loadingTitle.textContent = 'Loading Gaussian scene…';
  loadingDetail.textContent = `Downloading ${$('ply-size').textContent} and preparing splats in your browser.`;
  loadingProgress.style.width = '0%';
  loadingProgressTrack.setAttribute('aria-valuenow', '0');
  progressivePreviewReady = false;
  status.textContent = 'Loading Gaussian PLY…';

  try {
    viewer = new GaussianSplats3D.Viewer({
      rootElement: viewerRoot,
      // The reconstructed scene uses Y as its vertical axis and camera
      // forward is +Z. Start from a front-facing view near the camera path;
      // the old high, tilted view looked past the room and appeared empty.
      cameraUp: [0, -1, 0],
      initialCameraPosition: [-0.3, -0.45, -2.8],
      initialCameraLookAt: [-0.3, 0, -0.2],
      sharedMemoryForWorkers: false,
      // This library's GPU-distance sort path expects worker-shared buffers;
      // with shared memory disabled, the worker receives no splat indexes and
      // the canvas stays black. Use its WASM worker sort path instead.
      gpuAcceleratedSort: false,
      dynamicScene: false,
      enableSIMDInSort: true,
      integerBasedSort: false,
      antialiased: true,
      sphericalHarmonicsDegree: Number(shControl.value),
    });
    viewer.start();

    await viewer.addSplatScene(plyUrl, {
      format: GaussianSplats3D.SceneFormat.Ply,
      splatAlphaRemovalThreshold: alphaThreshold,
      showLoadingUI: true,
      progressiveLoad: true,
      onProgress: updateLoadProgress,
    });
    progressivePreviewReady = true;
    resetButton.disabled = false;
    status.textContent = 'First splats received · rendering preview while the rest loads';
    loadingTitle.textContent = 'Preview is rendering while the full scene loads…';
    loadingDetail.textContent = `Downloading the rest of the ${$('ply-size').textContent} scene.`;
    await waitForProgressiveSceneBuild();
    loading.classList.add('hidden');
    status.textContent = `Interactive ${modelVariant} scene loaded · iteration ${iteration} · opacity ${alphaThreshold}/255`;
  } catch (error) {
    console.error(error);
    if (viewer) {
      await viewer.dispose().catch(() => {});
      viewer = null;
    }
    loading.classList.add('hidden');
    welcome.classList.remove('hidden');
    loadButton.disabled = false;
    loadButton.textContent = 'Retry interactive 3D';
    status.textContent = `Viewer error: ${error.message}`;
  } finally {
    sceneLoading = false;
    setControlsDisabled(false);
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
