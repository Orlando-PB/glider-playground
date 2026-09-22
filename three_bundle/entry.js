// Source for static/vendor/globe-three.min.js: globe.gl and three built together, so the globe and the pages' own
// layers share one copy of three. Defines window.THREE (with OrbitControls) and window.Globe. Rebuild after
// bumping a version in package.json:
//   cd three_bundle && npm install && npm run build
import * as THREE_NS from 'three';
import { OrbitControls } from 'three/examples/jsm/controls/OrbitControls.js';
import Globe from 'globe.gl';

window.THREE = { ...THREE_NS, OrbitControls };
window.Globe = Globe;
