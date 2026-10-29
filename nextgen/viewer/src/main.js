import * as GaussianSplats3D from '@mkkellogg/gaussian-splats-3d';
import './style.css';
import { loadMesh } from './mesh-viewer';
import { prepareModelCache, modelIsCached } from './model-cache';

const params = new URLSearchParams(window.location.search);
const selectedScene = params.get('scene');
const scene = selectedScene || 'scene';
let displaySceneName = scene.replaceAll('_', ' ');
const staticDeploy = import.meta.env.VITE_STATIC_DEPLOY === '1';
const appBase = new URL(import.meta.env.BASE_URL, window.location.href);
const siteUrl = (path) => new URL(path, appBase).toString();
const sceneAssetUrl = (path) => staticDeploy
  ? siteUrl(`scene/${encodeURIComponent(scene)}/${path.split('/').map(encodeURIComponent).join('/')}`)
  : `/scene/${encodeURIComponent(scene)}/${path.split('/').map(encodeURIComponent).join('/')}`;
const catalogUrl = staticDeploy ? siteUrl('scenes.json') : '/api/scenes';
let staticRenders = null;
if (staticDeploy) document.getElementById('gallery-button').href = appBase.toString();
const iteration = params.get('iter') || '10000';
const supportedThresholds = [1, 5, 10];
const requestedThreshold = Number(params.get('alpha') || '5');
let alphaThreshold = supportedThresholds.includes(requestedThreshold) ? requestedThreshold : 5;
const requestedShDegree = Number(params.get('sh') || '2');
const shDegree = [0, 1, 2].includes(requestedShDegree) ? requestedShDegree : 2;
const requestedModel = params.get('model');
const meshVariants = ['truth', 'truth-raw', 'pretty', 'sugar', 'sugar-repaired', 'sugar-enhanced'];
let isMesh = meshVariants.includes(requestedModel);
let modelVariant = ['optimized', 'cleaned', 'depth-aware', ...meshVariants].includes(requestedModel) ? requestedModel : 'trained';

const modelPaths = {
  optimized: sceneAssetUrl('09_web/scene.spz'),
  truth: sceneAssetUrl('08_mesh/truth/truth_tsdf_colmap.ply'),
  'truth-raw': sceneAssetUrl('08_mesh/truth/truth_tsdf_raw_colmap.ply'),
  pretty: sceneAssetUrl('08_mesh/presentation/presentation_mesh_colmap.ply'),
  sugar: sceneAssetUrl('08_mesh/sugar/final/sugar_textured_mesh.glb'),
  'sugar-repaired': sceneAssetUrl('08_mesh/sugar/repaired/sugar_repaired.glb'),
  'sugar-enhanced': sceneAssetUrl('08_mesh/sugar/enhanced/sugar_enhanced.glb'),
  cleaned: sceneAssetUrl('08_postprocess/cleaned.ply'),
  'depth-aware': sceneAssetUrl('08_postprocess/depth_aware_cleaned.ply'),
};
let basePlyUrl = modelPaths[modelVariant]
  || sceneAssetUrl(`06_gs_model/point_cloud/iteration_${iteration}/point_cloud.ply`);
let plyUrl = basePlyUrl;
const rendersUrl = sceneAssetUrl('07_renders/renders/');

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

$('scene-title').textContent = displaySceneName;
$('welcome-title').textContent = displaySceneName;
$('iteration-value').textContent = iteration;
document.title = `${displaySceneName} · Namma NextGen`;
shControl.value = String(shDegree);
alphaControl.value = String(alphaThreshold);
$('model-variant').value = modelVariant;
const modelControl = $('model-variant');
const defaultWelcome = document.querySelector('#welcome p').textContent;
function syncModelControls() {
  modelControl.value = modelVariant;
  $('mesh-style-control').hidden = !isMesh;
  shControl.closest('label').hidden = isMesh;
  alphaControl.closest('label').hidden = isMesh;
  document.querySelector('.badge').textContent = isMesh ? `${modelVariant} mesh` : 'Interactive Gaussian viewer';
  document.querySelector('#welcome p').textContent = isMesh
    ? 'Inspect reconstructed surfaces with color, solid shading, normals or wireframe. All mesh variants use the COLMAP coordinate frame.'
    : defaultWelcome;
}
syncModelControls();
modelControl.addEventListener('change', async (event) => {
  if (sceneLoading) return;
  const nextModel = event.target.value;
  setControlsDisabled(true);
  sceneLoading = true;
  try {
    if (viewer) await viewer.dispose();
    viewer = null;
    viewerRoot.replaceChildren();
    modelVariant = nextModel;
    isMesh = meshVariants.includes(modelVariant);
    basePlyUrl = modelPaths[modelVariant]
      || sceneAssetUrl(`06_gs_model/point_cloud/iteration_${iteration}/point_cloud.ply`);
    const url = new URL(location.href);
    url.searchParams.set('model', modelVariant);
    history.replaceState({}, '', url);
    syncModelControls();
    resetButton.disabled = true;
    loadButton.disabled = false;
    welcome.classList.add('hidden');
    if (await probePly()) await loadScene();
    else welcome.classList.remove('hidden');
  } catch (error) {
    status.textContent = `Could not switch model: ${error.message}`;
    welcome.classList.remove('hidden');
  } finally {
    sceneLoading = false;
    setControlsDisabled(false);
  }
});

let viewer = null;
let renderNames = [];
let sceneLoading = false;
let progressivePreviewReady = false;
let assetCached = false;

function updateLoadProgress(percent, label, loaderStatus) {
  const safePercent = Math.max(0, Math.min(100, Number(percent) || 0));
  const displayedPercent = Math.floor(safePercent);
  loadingProgress.style.width = `${safePercent}%`;
  loadingProgressTrack.setAttribute('aria-valuenow', String(displayedPercent));

  if (loaderStatus === 1) {
    loadingTitle.textContent = 'Preparing the scene for rendering…';
    loadingDetail.textContent = 'Download complete. Building GPU buffers and ordering splats; large scenes can take a little longer at this step.';
    loadingProgressTrack.classList.add('indeterminate');
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
  const assetLabel = modelVariant === 'optimized' ? 'optimized SPZ' : 'original PLY';
  loadingDetail.textContent = `${assetCached ? 'Reading cached scene' : `Downloading ${assetLabel}`} · ${displayedPercent}% · asset size ${$('ply-size').textContent}. Decoded GPU/render memory can be larger than the downloaded file. ${progressivePreviewReady ? 'The preview is building as data arrives.' : 'This large scene may take a few minutes on first load.'}`;
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
  modelControl.disabled = disabled;
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
    versionedUrl.searchParams.set('scene', scene);
    versionedUrl.searchParams.set('v', version.replaceAll('"', ''));
    plyUrl = versionedUrl.toString();
    assetCached = await modelIsCached(plyUrl);
    $('ply-size').textContent = `${bytes(length)}${assetCached ? ' · cached' : ''}`;
    return true;
  } catch (error) {
    $('ply-size').textContent = 'missing';
    loadButton.disabled = true;
    loadButton.textContent = isMesh ? 'Mesh asset not found' : 'Gaussian asset not found';
    status.textContent = `Gaussian asset unavailable: ${error.message}`;
    return false;
  }
}

async function discoverRenders() {
  try {
    if (staticDeploy) {
      renderNames = staticRenders || [];
      $('render-count').textContent = String(renderNames.length);
      return;
    }
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

async function discoverAvailableModels() {
  try {
    const response = await fetch(catalogUrl, { cache: 'no-store' });
    if (!response.ok) return;
    const catalog = await response.json();
    const current = catalog.find((item) => item.name === scene);
    if (!current) return;
    if (staticDeploy) {
      staticRenders = current.renders || [];
      for (const model of current.models) {
        if (model.asset) modelPaths[model.id] = sceneAssetUrl(model.asset);
      }
      if (modelPaths[modelVariant]) basePlyUrl = modelPaths[modelVariant];
    }
    displaySceneName = current.display_name || displaySceneName;
    $('scene-title').textContent = displaySceneName;
    $('welcome-title').textContent = displaySceneName;
    document.title = `${displaySceneName} · Namma NextGen`;
    const available = new Set(current.models.map((item) => item.id));
    if (!requestedModel && available.has('optimized')) {
      modelVariant = 'optimized';
      isMesh = false;
      basePlyUrl = modelPaths.optimized;
      modelControl.value = modelVariant;
      syncModelControls();
    }
    for (const option of modelControl.options) {
      option.hidden = !available.has(option.value);
      option.disabled = !available.has(option.value);
    }
    if (!available.has(modelVariant)) {
      modelVariant = 'trained';
      isMesh = false;
      basePlyUrl = sceneAssetUrl(`06_gs_model/point_cloud/iteration_${current.iteration}/point_cloud.ply`);
      modelControl.value = modelVariant;
      syncModelControls();
      const url = new URL(window.location.href);
      url.searchParams.set('model', modelVariant);
      url.searchParams.set('iter', String(current.iteration));
      history.replaceState({}, '', url);
    }
  } catch {
    // Keep the legacy direct-scene behavior when the catalog is unavailable.
  }
}

async function loadScene() {
  if (viewer) return;
  await prepareModelCache();
  if (isMesh) {
    sceneLoading = true;
    setControlsDisabled(true);
    loadButton.disabled = true;
    welcome.classList.add('hidden');
    loading.classList.remove('hidden');
    loadingTitle.textContent = `Loading ${modelVariant} mesh…`;
    try {
      viewer = await loadMesh(viewerRoot, plyUrl, ['pretty', 'sugar', 'sugar-repaired', 'sugar-enhanced'].includes(modelVariant), (event) => {
        loadingDetail.textContent = `${assetCached ? 'Reading cached model' : 'Downloading model'} · ${bytes(event.loaded)}`;
      });
      $('mesh-style').value = ['pretty', 'sugar', 'sugar-repaired', 'sugar-enhanced'].includes(modelVariant) ? 'color' : 'solid';
      viewer.setStyle($('mesh-style').value);
      $('mesh-style').onchange = (event) => viewer.setStyle(event.target.value);
      resetButton.disabled = false;
      status.textContent = `${modelVariant} mesh loaded · COLMAP coordinates`;
    } catch (error) {
      welcome.classList.remove('hidden');
      loadButton.disabled = false;
      status.textContent = `Mesh unavailable: ${error.message}`;
    } finally {
      loading.classList.add('hidden');
      sceneLoading = false;
      setControlsDisabled(false);
    }
    return;
  }
  sceneLoading = true;
  setControlsDisabled(true);
  loadButton.disabled = true;
  welcome.classList.add('hidden');
  loading.classList.remove('hidden');
  loadingTitle.textContent = 'Loading Gaussian scene…';
  loadingDetail.textContent = `${assetCached ? 'Reading cached model' : `Downloading ${modelVariant === 'optimized' ? 'optimized SPZ' : 'original PLY'}`} · asset size ${$('ply-size').textContent}. Preparing splats in your browser; decoded GPU/render memory can be larger.`;
  loadingProgress.style.width = '0%';
  loadingProgressTrack.classList.remove('indeterminate');
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
      // Use the established worker sort path: the GPU distance path can
      // produce an empty view on some browser/GPU combinations.
      sharedMemoryForWorkers: false,
      gpuAcceleratedSort: false,
      dynamicScene: false,
      enableSIMDInSort: true,
      integerBasedSort: false,
      antialiased: true,
      sphericalHarmonicsDegree: Number(shControl.value),
    });
    viewer.start();

    await viewer.addSplatScene(plyUrl, {
      format: new URL(plyUrl).pathname.toLowerCase().endsWith('.spz') ? GaussianSplats3D.SceneFormat.Spz : GaussianSplats3D.SceneFormat.Ply,
      splatAlphaRemovalThreshold: alphaThreshold,
      showLoadingUI: true,
      progressiveLoad: true,
      onProgress: updateLoadProgress,
    });
    progressivePreviewReady = true;
    resetButton.disabled = false;
    status.textContent = 'First splats received · rendering preview while the rest loads';
    loadingTitle.textContent = 'Preview is rendering while the full scene loads…';
    loadingDetail.textContent = `${assetCached ? 'Reading cached model' : 'Downloading model'} · ${$('ply-size').textContent}.`;
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
    status.textContent = /webgl context/i.test(error.message)
      ? 'Could not start WebGL. Check browser hardware acceleration, or view the rendered frames.'
      : `Viewer error: ${error.message}`;
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

function initGallery() {
  const grid = $('scene-grid');
  const search = $('scene-search');
  const count = $('scene-count');
  const empty = $('gallery-empty');
  const error = $('gallery-error');
  let scenes = [];

  const render = () => {
    const query = search.value.trim().toLowerCase();
    const visible = scenes.filter((item) =>
      `${item.display_name || ''} ${item.description || ''} ${item.name}`.toLowerCase().includes(query));
    grid.replaceChildren();
    count.textContent = `${visible.length} reconstruction${visible.length === 1 ? '' : 's'}`;
    empty.classList.toggle('hidden', scenes.length > 0 && visible.length > 0);
    if (scenes.length > 0 && visible.length === 0) {
      empty.textContent = 'No scene matches that search.';
      empty.classList.remove('hidden');
    } else {
      empty.textContent = 'No completed reconstructions were found. Finish training a scene, then reload this page.';
    }

    for (const item of visible) {
      const card = document.createElement('article');
      card.className = 'scene-card';

      const preview = document.createElement('div');
      preview.className = 'scene-preview';
      const placeholder = document.createElement('span');
      placeholder.className = 'preview-placeholder';
      placeholder.textContent = 'Namma reconstruction';
      preview.appendChild(placeholder);
      if (item.thumbnail) {
        const image = document.createElement('img');
        image.loading = 'lazy';
        image.alt = `${item.display_name || item.name} preview`;
        image.src = staticDeploy ? siteUrl(item.thumbnail) : item.thumbnail;
        image.addEventListener('load', () => placeholder.remove(), { once: true });
        image.addEventListener('error', () => image.remove(), { once: true });
        preview.appendChild(image);
      }
      card.appendChild(preview);

      const body = document.createElement('div');
      body.className = 'scene-body';
      const title = document.createElement('h2');
      title.className = 'scene-name';
      title.textContent = item.display_name || item.name.replaceAll('_', ' ');
      const meta = document.createElement('div');
      meta.className = 'scene-meta';
      meta.textContent = item.optimized_bytes
        ? `Iteration ${item.iteration} · ${item.render_count} renders · ${bytes(item.optimized_bytes)} optimized · ${bytes(item.trained_bytes)} ${staticDeploy ? 'full detail' : 'original PLY'}`
        : `Iteration ${item.iteration} · ${item.render_count} renders · ${bytes(item.trained_bytes)} trained PLY`;
      body.append(title, meta);
      if (item.description) {
        const description = document.createElement('p');
        description.className = 'scene-description';
        description.textContent = item.description;
        body.appendChild(description);
      }

      const modelButtons = document.createElement('div');
      modelButtons.className = 'scene-model-buttons';
      const availableCardModels = item.name === 'retail_competition_160'
        ? item.models.filter((model) => ['trained', 'optimized', 'sugar'].includes(model.id))
        : item.models;
      const cardModels = [...availableCardModels].sort((a, b) =>
        Number(b.id === 'optimized') - Number(a.id === 'optimized'));
      for (const model of cardModels) {
        const open = document.createElement('button');
        open.className = model.id === 'optimized' ? 'primary scene-model-button' : 'secondary scene-model-button';
        open.textContent = model.id === 'sugar' ? 'SuGaR Final' : model.label;
        open.addEventListener('click', () => {
        const target = staticDeploy ? new URL(appBase) : new URL('/viewer/', window.location.origin);
        target.searchParams.set('scene', item.name);
        target.searchParams.set('iter', String(item.iteration));
        target.searchParams.set('model', model.id);
        window.location.href = target.toString();
        });
        modelButtons.appendChild(open);
      }
      body.appendChild(modelButtons);
      card.appendChild(body);
      grid.appendChild(card);
    }
  };

  search.addEventListener('input', render);
  fetch(catalogUrl, { cache: 'no-store' })
    .then((response) => {
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      return response.json();
    })
    .then((catalog) => {
      scenes = Array.isArray(catalog) ? catalog : [];
      render();
    })
    .catch(() => {
      count.textContent = 'catalog unavailable';
      error.classList.remove('hidden');
    });
}

loadButton.addEventListener('click', loadScene);
resetButton.addEventListener('click', resetView);
$('renders-button').addEventListener('click', openRenders);
$('open-renders-button').addEventListener('click', openRenders);
$('close-renders').addEventListener('click', () => rendersPanel.classList.add('hidden'));

setControlsDisabled(true);
if (!selectedScene) {
  document.body.classList.add('gallery-mode');
  if (staticDeploy) document.querySelector('.gallery-footer').textContent = 'Explore these reconstructions in your browser';
  $('gallery').classList.remove('hidden');
  document.title = 'Reconstruction Gallery · Namma';
  initGallery();
} else {
  discoverAvailableModels().then(() => Promise.all([probePly(), discoverRenders()])).then(() => {
    setControlsDisabled(false);
    status.textContent = `Ready · ${displaySceneName}`;
  });
}
