// Which swimming animals belong where: the species table for the three.js views' scenery (scenery.js places and
// animates them; seabed life and plants were dropped — an occasional passer-by reads better than a furnished seabed). Carried over from the Plotly 3D view's 3d_view/scenery/scenery.js; models are that folder's
// _bundle.json (metres, +x nose, +z up).
//
// Rule fields. float: [min,max] depth (m) in the water column (+ surface: true rides at the surface).
// lat: |latitude| band, or region: [[lonMin, lonMax, latMin, latMax], ...] signed-degree boxes.
// size: on-screen height in scene units (scene ~1 across; glider 0.03). density: fraction of eligible cells; max: cap.
// shoal: true — usually comes as a small loose group (scenery.js SHOAL).
// swim: { r, period, bob } — stateless elliptical loop of radius ~r (scene units) per `period` seconds.
// Floating kinds use the factories below: model, real length (m), |lat| band, depth band (m), overrides.
const M_PER_UNIT = 0.0043;    // scene units per metre of animal (14 m humpback → 0.06)
// `group` picks the animal's motion (scenery.js MOTION) and keeps a scene's cast varied.
const whale  = (model, L, lat, float, o = {}) => ({ model, group: 'whale', lat, float, size: L * M_PER_UNIT, density: 0.03, max: 1, swim: { r: 0.25, period: 120, bob: 0.1 }, ...o });
const shark  = (model, L, lat, float, o = {}) => ({ model, group: 'shark', lat, float, size: L * M_PER_UNIT, density: 0.03, max: 2, swim: { r: 0.15, period: 70, bob: 0.1 }, ...o });
const school = (model, lat, float, o = {}) => ({ model, group: 'school', lat, float, size: 0.02, density: 0.05, max: 25, swim: { r: 0.08, period: 45, bob: 0.2 }, ...o });
const jelly  = (model, lat, float, o = {}) => ({ model, group: 'jelly', lat, float, size: 0.008, density: 0.015, max: 6, swim: { r: 0.01, period: 50, bob: 0.6 }, ...o });
// Everything else that swims or sits: explicit on-screen size (small animals are drawn larger than life, like the jellies).
const animal = (group, model, size, lat, where, o = {}) => ({ model, group, lat, ...where, size, density: 0.05, max: 4, ...o });
const cruise = (r, period, bob = 0.15) => ({ swim: { r, period, bob } });
// Ocean boxes [lonMin, lonMax, latMin, latMax] shared by the regional species below.
const NE_ATLANTIC = [-30, 42, 28, 72], NW_ATLANTIC = [-80, -45, 35, 62], CARIBBEAN = [-100, -55, 5, 33];
const N_PACIFIC = [[-180, -115, 32, 66], [125, 180, 32, 66]], INDO_PACIFIC = [[30, 180, -30, 30], [-180, -120, -30, 30]];
const SOUTHERN = [-180, 180, -80, -55];

// Who eats whom, so a chase (scenery.js) is one that happens: hunter model -> prey, as model names or 'group:<group>'.
// Filter feeders (blue, sei, basking and whale sharks), grazers (green turtle) and sponge-eaters (hawksbill) hunt nothing here.
const FORAGE = ['fish_herring', 'fish_capelin', 'fish_sardine', 'fish_anchovy'], SMALL_SQUID = ['squid_common'];
export const EATS = {
    whale_orca: ['porpoise_harbour', 'dolphin_common', 'fish_herring', 'fish_mackerel', 'fish_tuna', 'group:ray'],
    whale_sperm: ['squid_giant', 'squid_colossal', 'squid_humboldt'],
    whale_pilot: ['squid_common', 'squid_humboldt', 'fish_mackerel'],
    whale_beluga: ['fish_capelin', 'fish_cod'], whale_narwhal: ['fish_cod', 'squid_humboldt'],
    whale_humpback: FORAGE, whale_minke: FORAGE, whale_fin: ['fish_herring', 'fish_capelin'],
    dolphin_common: [...FORAGE, 'fish_mackerel', ...SMALL_SQUID], dolphin_bottlenose: [...FORAGE, 'fish_mackerel', ...SMALL_SQUID], dolphin_whitebeaked: ['fish_herring', 'fish_mackerel', 'fish_cod'],
    dolphin_spinner: ['fish_lanternfish', ...SMALL_SQUID], dolphin_hourglass: ['fish_lanternfish', ...SMALL_SQUID], porpoise_harbour: ['fish_herring', 'fish_capelin'],
    shark_white: ['fish_tuna', 'porpoise_harbour', 'turtle_loggerhead', 'group:ray'], shark_blue: ['fish_mackerel', 'fish_herring', ...SMALL_SQUID, 'squid_humboldt'],
    shark_porbeagle: ['fish_mackerel', 'fish_herring', ...SMALL_SQUID], shark_hammerhead: ['ray_eagle', 'fish_sardine', ...SMALL_SQUID], shark_greenland: ['fish_cod'],
    fish_tuna: ['fish_sardine', 'fish_anchovy', 'fish_mackerel', ...SMALL_SQUID], fish_cod: ['fish_capelin', 'fish_herring'], fish_mackerel: ['fish_anchovy', 'fish_sardine'],
    squid_humboldt: ['fish_lanternfish', 'fish_sardine'], squid_giant: ['fish_lanternfish'], squid_colossal: ['fish_lanternfish'], squid_common: ['fish_anchovy', 'fish_sardine'],
    turtle_leatherback: ['group:jelly'], turtle_loggerhead: ['group:jelly'], mola_mola: ['group:jelly'],
};

export const RULES = [

    // ── Whales & dolphins (model, length m, |lat|, depth m) ──
    whale('whale_humpback', 14, [0, 75],  [3, 60]),
    whale('whale_minke',     8, [30, 80], [3, 60],  { max: 2 }),
    whale('whale_fin',      20, [20, 75], [5, 100]),
    whale('whale_sei',      15, [20, 65], [5, 100]),
    whale('whale_blue',     25, [0, 70],  [5, 100], { density: 0.005 }),
    whale('whale_sperm',    16, [0, 70],  [50, 800], { swim: { r: 0.2, period: 150, bob: 0.3 } }),
    whale('whale_orca',      7, [0, 80],  [3, 50],  { max: 2, swim: { r: 0.2, period: 60, bob: 0.15 } }),
    whale('whale_pilot',     6, [20, 65], [10, 300], { max: 2 }),
    whale('whale_beluga',  4.5, [60, 82], [2, 40],  { max: 3 }),
    whale('whale_narwhal', 4.5, [65, 85], [5, 300], { max: 2 }),

    // ── Dolphins & porpoises: small pods, quick tight loops ──
    animal('dolphin', 'dolphin_common',      0.010, [0, 60],  { float: [1, 30] },  { ...cruise(0.15, 40, 0.3) }),
    animal('dolphin', 'dolphin_bottlenose',  0.013, [0, 60],  { float: [1, 30] },  { ...cruise(0.15, 45, 0.3) }),
    animal('dolphin', 'dolphin_whitebeaked', 0.012, null,     { float: [1, 40] },  { region: [[-75, 40, 45, 78]], ...cruise(0.15, 45, 0.3) }),   // cold N Atlantic only
    animal('dolphin', 'dolphin_spinner',     0.009, [0, 30],  { float: [1, 30] },  { ...cruise(0.12, 35, 0.4) }),
    animal('dolphin', 'dolphin_hourglass',   0.008, null,     { float: [1, 30] },  { region: [[-180, 180, -68, -45]], ...cruise(0.15, 40, 0.3) }), // Southern Ocean
    animal('dolphin', 'porpoise_harbour',    0.007, null,     { float: [1, 40] },  { region: [[-180, 180, 32, 72]], ...cruise(0.1, 50, 0.15) }),  // shy, coastal, northern hemisphere

    // ── Sharks ──
    shark('shark_basking',    8, [30, 65], [5, 200], { max: 1 }),
    shark('shark_white',      5, [0, 60],  [5, 200], { max: 1 }),
    shark('shark_blue',       3, [0, 60],  [10, 300]),
    shark('shark_porbeagle',  2.5, [30, 70], [10, 300]),
    shark('shark_hammerhead', 3.5, [0, 40], [5, 150]),
    shark('shark_whale',     10, [0, 35], [2, 80],  { max: 1, swim: { r: 0.2, period: 140, bob: 0.05 } }),
    shark('shark_greenland',  4, [55, 82], [200, 1200], { swim: { r: 0.1, period: 180, bob: 0.05 } }),

    // ── Schooling fish ──
    school('fish_herring',    [30, 75], [20, 200]),
    school('fish_mackerel',   [25, 70], [10, 150]),
    school('fish_capelin',    [55, 80], [20, 200]),
    school('fish_cod',        [40, 75], [50, 300],  { size: 0.025, max: 10 }),
    school('fish_sardine',    [0, 50],  [10, 100]),
    school('fish_anchovy',    [0, 50],  [5, 80]),
    school('fish_tuna',       [0, 50],  [20, 300],  { size: 0.035, max: 6, swim: { r: 0.15, period: 35, bob: 0.2 } }),
    school('fish_lanternfish', [0, 70], [300, 1000], { size: 0.012, max: 30 }),

    // ── Sunfish, rays, turtles ──
    animal('mola', 'mola_mola',          0.012, [0, 62],  { float: [1, 200] }, { ...cruise(0.06, 150, 0.3) }),          // basks near the surface, summers as far north as the UK
    animal('ray', 'ray_manta',           0.014, [0, 35],  { float: [2, 100] }, cruise(0.18, 90, 0.2)),
    animal('ray', 'ray_eagle',           0.009, [0, 32],  { float: [1, 40] },  { ...cruise(0.12, 60, 0.2) }),
    animal('turtle', 'turtle_leatherback', 0.012, [0, 65], { float: [2, 300] }, cruise(0.12, 110)),                              // the one turtle of cold water
    animal('turtle', 'turtle_loggerhead',  0.009, [0, 45], { float: [1, 100] }, cruise(0.1, 100)),
    animal('turtle', 'turtle_green',       0.009, [0, 35], { float: [1, 40] },  cruise(0.08, 100)),
    animal('turtle', 'turtle_hawksbill',   0.008, [0, 28], { float: [1, 30] },  cruise(0.06, 100)),                              // coral reefs

    // ── Eels & octopuses ──
    animal('eel', 'eel_european',  0.009, null,    { float: [200, 1000] }, { region: [[-80, 30, 20, 68]], ...cruise(0.2, 160, 0.3) }),   // silver eels crossing to the Sargasso at depth
    animal('octopus', 'octopus_dumbo',         0.0035, [0, 80], { float: [1000, 4000] }, { swim: { r: 0.02, period: 120, bob: 0.8 } }),   // hovers over the deep seabed

    // ── Squid, cuttlefish, nautilus ──
    animal('squid', 'squid_common',   0.007, [0, 62], { float: [10, 300] },   { ...cruise(0.12, 50, 0.3) }),
    // More squid, sharing the two squid models (`shoal`: usually seen as a loose shoal rather than alone).
    animal('squid', 'squid_common',   0.008, null,    { float: [20, 400] },   { shoal: true, region: [[-30, 42, 28, 72]], ...cruise(0.12, 50, 0.3) }),      // veined squid, NE Atlantic shelf
    animal('squid', 'squid_humboldt', 0.009, null,    { float: [50, 800] },   { shoal: true, region: [[-45, 42, 30, 75]], ...cruise(0.12, 60, 0.4) }),      // European flying squid, out to Iceland and Norway
    animal('squid', 'squid_humboldt', 0.008, null,    { float: [50, 600] },   { shoal: true, region: [[-80, -40, 25, 65]], ...cruise(0.12, 60, 0.4) }),     // northern shortfin squid, NW Atlantic
    animal('squid', 'squid_common',   0.007, null,    { float: [5, 300] },    { shoal: true, region: [[-80, -55, 25, 48]], ...cruise(0.12, 50, 0.3) }),     // longfin inshore squid, US east coast
    animal('squid', 'squid_common',   0.006, null,    { float: [5, 200] },    { shoal: true, region: [[-135, -105, 20, 58]], ...cruise(0.12, 50, 0.3) }),   // market squid, NE Pacific
    animal('squid', 'squid_humboldt', 0.008, null,    { float: [20, 500] },   { shoal: true, region: [[118, 165, 25, 55]], ...cruise(0.12, 60, 0.4) }),     // Japanese flying squid
    animal('squid', 'squid_humboldt', 0.008, null,    { float: [50, 800] },   { shoal: true, region: [[-70, -40, -55, -30]], ...cruise(0.12, 60, 0.4) }),   // Argentine shortfin squid, Patagonian shelf
    animal('squid', 'squid_common',   0.007, null,    { float: [2, 100] },    { shoal: true, region: [[30, 180, -35, 35]], ...cruise(0.1, 60, 0.3) }),      // bigfin reef squid, Indo-Pacific
    animal('squid', 'squid_humboldt', 0.009, null,    { float: [100, 1000] }, { shoal: true, region: [[-180, 180, -65, -40]], ...cruise(0.12, 60, 0.4) }),  // Southern Ocean arrow squids
    // Deep-water squid, alone in the dark (lengths are drawn larger than life, like the rest).
    animal('squid', 'squid_common',   0.006, [0, 60], { float: [300, 1000] },  { ...cruise(0.08, 120, 0.4) }),      // jewel squid: twilight zone, world-wide
    animal('squid', 'squid_common',   0.007, [0, 75], { float: [200, 2000] },  { ...cruise(0.06, 160, 0.5) }),      // glass (cranch) squid
    animal('squid', 'squid_humboldt', 0.011, [0, 60], { float: [200, 1200] },  { max: 1, ...cruise(0.1, 140, 0.4) }),   // Dana octopus squid
    animal('squid', 'squid_giant',    0.010, [0, 65], { float: [600, 1500] },  { max: 1, ...cruise(0.06, 200, 0.3) }),  // vampire squid's depths: the oxygen minimum
    animal('squid', 'squid_giant',    0.016, [0, 70], { float: [2000, 4500] }, { max: 1, ...cruise(0.05, 260, 0.2) }),  // bigfin squid, trailing its arms over the abyss
    animal('squid', 'squid_humboldt', 0.009, [40, 80], { float: [400, 1500] }, { ...cruise(0.1, 140, 0.4) }),       // Gonatus: the deep squid of the cold north and south
    animal('squid', 'squid_humboldt', 0.010, null,    { float: [100, 700] },  { region: [[-130, -70, -45, 45]], ...cruise(0.12, 60, 0.4) }),   // E Pacific only
    animal('squid', 'squid_giant',    0.012, [0, 70], { float: [300, 1000] }, { max: 1, ...cruise(0.1, 200, 0.3) }),
    animal('squid', 'squid_colossal', 0.014, null,    { float: [1000, 2200] }, { max: 1, region: [[-180, 180, -78, -50]], ...cruise(0.08, 220, 0.3) }),  // Antarctic deep water
    // Cuttlefish: NE Atlantic/Med/W Africa and the Indo-West Pacific — there are none in the Americas.
    animal('squid', 'cuttlefish',     0.007, null,    { float: [2, 80] },     { region: [[-20, 42, -35, 60], [42, 180, -40, 40]], ...cruise(0.03, 90, 0.2) }),
    animal('squid', 'nautilus',       0.006, null,    { float: [100, 500] },  { region: [[90, 180, -30, 20]], swim: { r: 0.02, period: 120, bob: 0.8 } }),

    // ── Jellyfish (size = bell diameter on screen) ──
    jelly('jelly_moon',       [0, 70],  [1, 40],   { size: 0.006 }),                                              // Aurelia: near-global coastal
    jelly('jelly_lionsmane',  null,     [2, 80],   { size: 0.011, max: 4, region: [[-180, 180, 42, 80]] }),      // cold boreal/Arctic waters only
    jelly('jelly_compass',    null,     [2, 40],   { size: 0.006, region: [[-20, 40, 30, 62]] }),                 // NE Atlantic & Mediterranean
    jelly('jelly_barrel',     null,     [2, 40],   { size: 0.010, max: 4, region: [[-15, 42, 30, 60]] }),        // NE Atlantic, Med, Black Sea
    jelly('jelly_blue',       null,     [2, 30],   { size: 0.005, region: [[-25, 30, 45, 68]] }),                 // North Sea / NE Atlantic shelf
    jelly('jelly_friedegg',   null,     [1, 30],   { size: 0.006, region: [[-6, 36, 30, 46]] }),                  // Mediterranean
    jelly('jelly_mauve',      [0, 55],  [2, 150],  { size: 0.004 }),                                              // Pelagia: warm/temperate open ocean
    jelly('jelly_nettle',     null,     [2, 60],   { size: 0.008, region: [[-180, -110, 25, 60]] }),              // NE Pacific
    jelly('jelly_cannonball', null,     [1, 30],   { size: 0.005, region: [[-100, -60, 8, 40], [-60, -30, -30, 8]] }), // Gulf of Mexico, US SE coast to Brazil
    jelly('jelly_box',        null,     [1, 15],   { size: 0.005, max: 8, region: [[95, 160, -25, 20]] }),        // Chironex: N Australia / Indo-West Pacific shallows
    jelly('jelly_antarctic',  null,     [2, 150],  { size: 0.007, region: [[-180, 180, -78, -55]] }),             // Diplulmaris: Southern Ocean
    jelly('jelly_helmet',     [0, 80],  [200, 1500], { size: 0.006, max: 5, swim: { r: 0.01, period: 80, bob: 1.5 } }),  // Periphylla: deep, migrates vertically
    jelly('jelly_atolla',     [0, 75],  [500, 3000], { size: 0.005, max: 5 }),                                   // deep-sea crown jelly
    // Man o' war: drifts AT the surface (sail above, tentacles below), warm and temperate seas.
    jelly('manowar',          [0, 50],  [0, 1],    { size: 0.006, max: 5, surface: true, swim: { r: 0.05, period: 200, bob: 0 } }),
];
