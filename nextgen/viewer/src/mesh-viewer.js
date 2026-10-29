import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { PLYLoader } from 'three/addons/loaders/PLYLoader.js';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';

export async function loadMesh(root, url, colored, progress) {
  const surfaces = [];
  if (new URL(url, location.href).pathname.endsWith('.glb')) {
    const gltf = await new GLTFLoader().loadAsync(url, progress);
    gltf.scene.updateMatrixWorld(true);
    gltf.scene.traverse((node) => {
      if (!node.isMesh) return;
      const geometry = node.geometry;
      geometry.applyMatrix4(node.matrixWorld);
      surfaces.push({ geometry, texture: node.material.map || null });
    });
    if (!surfaces.length) throw new Error('The asset contains no mesh surfaces');
  } else {
    surfaces.push({ geometry: await new PLYLoader().loadAsync(url, progress), texture: null });
  }
  const scene = new THREE.Scene();
  scene.background = new THREE.Color('#11151c');
  const camera = new THREE.PerspectiveCamera(55, 1, 0.001, 1000);
  camera.up.set(0, -1, 0);
  const renderer = new THREE.WebGLRenderer({ antialias: true });
  renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
  root.appendChild(renderer.domElement);
  const group = new THREE.Group();
  for (const surface of surfaces) {
    const { geometry, texture } = surface;
    if (!geometry.hasAttribute('normal')) geometry.computeVertexNormals();
    surface.solid = new THREE.MeshStandardMaterial({
      color: 0xb8bdc7, side: THREE.DoubleSide, roughness: 0.9,
    });
    surface.color = new THREE.MeshBasicMaterial({
      color: 0xffffff, map: texture,
      vertexColors: !texture && geometry.hasAttribute('color'), side: THREE.DoubleSide,
    });
    surface.normals = new THREE.MeshNormalMaterial({ side: THREE.DoubleSide });
    surface.mesh = new THREE.Mesh(geometry, colored ? surface.color : surface.solid);
    group.add(surface.mesh);
  }
  scene.add(group);
  scene.add(new THREE.HemisphereLight(0xffffff, 0x444455, 2));
  const light = new THREE.DirectionalLight(0xffffff, 2);
  camera.add(light);
  scene.add(camera);
  const controls = new OrbitControls(camera, renderer.domElement);
  const { center, radius } = new THREE.Box3().setFromObject(group).getBoundingSphere(new THREE.Sphere());
  controls.target.copy(center);
  camera.position.copy(center).add(new THREE.Vector3(0, -0.3, -2.6).multiplyScalar(radius));
  camera.far = Math.max(100, radius * 100);
  camera.updateProjectionMatrix();
  controls.update();
  const resize = () => {
    const w = root.clientWidth || innerWidth;
    const h = root.clientHeight || innerHeight;
    renderer.setSize(w, h);
    camera.aspect = w / h;
    camera.updateProjectionMatrix();
  };
  window.addEventListener('resize', resize);
  resize();
  renderer.setAnimationLoop(() => { controls.update(); renderer.render(scene, camera); });
  return {
    dispose() {
      renderer.setAnimationLoop(null);
      window.removeEventListener('resize', resize);
      controls.dispose();
      for (const surface of surfaces) {
        surface.geometry.dispose();
        surface.solid.dispose();
        surface.color.dispose();
        surface.normals.dispose();
        surface.texture?.dispose();
      }
      renderer.dispose();
      renderer.domElement.remove();
    },
    setStyle(style) {
      for (const surface of surfaces) {
        surface.mesh.material = style === 'normals' ? surface.normals
          : style === 'color' ? surface.color : surface.solid;
        surface.solid.wireframe = style === 'wireframe';
      }

    },
  };
}
