/** One place to tidy unit strings for display: CF/UDUNITS spellings -> what people write.
 *  window.fmtUnits('microMoleQuanta/m^2/sec') -> 'µmol quanta m⁻² s⁻¹', 'kg/m3' -> 'kg/m³', 'm-1' -> 'm⁻¹',
 *  'degree_Celsius' -> '°C'; placeholders ('1', 'No units', 'Dmnless', 'seconds since …') -> ''.
 *  window.withUnits('TEMP', 'degC') -> 'TEMP (°C)'. Plain Unicode, so it works in Plotly titles, DOM text and tooltips alike.
 */
(() => {
    'use strict';

    const PLACEHOLDER = new Set(['', 'no units', 'none', 'null', 'nan', '1', '1.0', 'dimensionless', 'dmnless', 'unitless', 'n/a', '-']);

    // Base-unit spellings (lower-cased lookup). Values may contain spaces ('µmol quanta').
    const ALIAS = {
        degree_celsius: '°C', degrees_celsius: '°C', degc: '°C', degree_c: '°C', deg_c: '°C', celsius: '°C', '°c': '°C',
        degree_fahrenheit: '°F', degrees_fahrenheit: '°F', degf: '°F', deg_f: '°F', fahrenheit: '°F',
        kelvin: 'K',
        degree: '°', degrees: '°', deg: '°', degs: '°',
        degree_north: '°N', degrees_north: '°N', degree_n: '°N', degree_south: '°S', degrees_south: '°S',
        degree_east: '°E', degrees_east: '°E', degree_e: '°E', degree_west: '°W', degrees_west: '°W',
        radian: 'rad', radians: 'rad',
        decibar: 'dbar', decibars: 'dbar', millibar: 'mbar', pascal: 'Pa', pascals: 'Pa',
        metre: 'm', metres: 'm', meter: 'm', meters: 'm',
        centimetre: 'cm', centimetres: 'cm', centimeter: 'cm', centimeters: 'cm',
        millimetre: 'mm', millimetres: 'mm', millimeter: 'mm', millimeters: 'mm',
        kilometre: 'km', kilometres: 'km', kilometer: 'km', kilometers: 'km',
        nanometre: 'nm', nanometres: 'nm', nanometer: 'nm', nanometers: 'nm',
        second: 's', seconds: 's', sec: 's', secs: 's', minute: 'min', minutes: 'min', hour: 'h', hours: 'h', hr: 'h', day: 'd', days: 'd',
        hertz: 'Hz', volt: 'V', volts: 'V', ampere: 'A', amperes: 'A', amp: 'A', amps: 'A', watt: 'W', watts: 'W', joule: 'J', joules: 'J',
        mho: 'S', mhos: 'S', siemens: 'S',
        mole: 'mol', moles: 'mol', millimole: 'mmol', millimoles: 'mmol', micromole: 'µmol', micromoles: 'µmol', micromol: 'µmol', umol: 'µmol',
        nanomole: 'nmol', nanomoles: 'nmol', nmole: 'nmol',
        micromolequanta: 'µmol quanta', micromolesquanta: 'µmol quanta', umolquanta: 'µmol quanta', quanta: 'quanta', photons: 'photons',
        gram: 'g', grams: 'g', kilogram: 'kg', kilograms: 'kg', milligram: 'mg', milligrams: 'mg', microgram: 'µg', micrograms: 'µg', ug: 'µg',
        litre: 'L', litres: 'L', liter: 'L', liters: 'L', l: 'L', millilitre: 'mL', milliliter: 'mL', ml: 'mL',
        steradian: 'sr', steradians: 'sr',
        percent: '%', psu: 'PSU', pss78: 'PSU', 'pss-78': 'PSU', ntu: 'NTU', ppb: 'ppb', ppm: 'ppm', count: 'count', counts: 'count',
    };

    // Chemical formulae whose digits are subscripts rather than exponents.
    const CHEM = new Set(['O2', 'CO2', 'N2', 'H2O', 'NO3', 'NO2', 'PO4', 'SiO4', 'NH4', 'CH4', 'H2S', 'CaCO3']);

    const SUP = { '-': '⁻', 0: '⁰', 1: '¹', 2: '²', 3: '³', 4: '⁴', 5: '⁵', 6: '⁶', 7: '⁷', 8: '⁸', 9: '⁹' };
    const SUB = { 0: '₀', 1: '₁', 2: '₂', 3: '₃', 4: '₄', 5: '₅', 6: '₆', 7: '₇', 8: '₈', 9: '₉' };
    const sup = n => String(n).split('').map(c => SUP[c] ?? c).join('');

    function base(name) {
        const key = name.toLowerCase();
        if (ALIAS[key]) return ALIAS[key];
        if (CHEM.has(name)) return name.replace(/\d/g, c => SUB[c]);
        if (key.startsWith('micro') && ALIAS[key.slice(5)]) return 'µ' + ALIAS[key.slice(5)];
        if (key.startsWith('milli') && ALIAS[key.slice(5)]) return 'm' + ALIAS[key.slice(5)];
        if (key.startsWith('kilo') && ALIAS[key.slice(4)]) return 'k' + ALIAS[key.slice(4)];
        return name;
    }

    // One factor: 'm', 'm-1', 'm^2', 'm**-2', 'm3' -> {base, exp}.
    function factor(tok) {
        if (ALIAS[tok.toLowerCase()] || CHEM.has(tok)) return { base: base(tok), exp: 1 };
        const m = /^([A-Za-zµ°%][A-Za-z_µ°%]*)(?:\^|\*\*)?(-?\d+)$/.exec(tok);
        return m ? { base: base(m[1]), exp: Number(m[2]) } : { base: base(tok), exp: 1 };
    }

    function fmtUnits(raw) {
        let u = String(raw ?? '').trim();
        if (PLACEHOLDER.has(u.toLowerCase())) return '';
        if (/\bsince\b/i.test(u)) return '';                         // time encodings, never shown
        u = u.replace(/^\((.*)\)$/, '$1').trim();
        const parts = u.split(/(\/)|\s+|\.(?!\d)|(?<!\*)\*(?!\*)/).filter(p => p);
        const num = [], den = [];
        let neg = false;
        for (const p of parts) {
            if (p === '/') { neg = true; continue; }
            if (p.toLowerCase() === 'per') { neg = true; continue; }
            if (!/^[A-Za-zµ°%][\w\-^*°µ%]*$/.test(p)) return u;    // something we don't understand: leave as written
            const f = factor(p);
            if (neg) f.exp = -f.exp;
            (f.exp < 0 ? den : num).push(f);
        }
        if (!num.length && !den.length) return u;
        const show = f => f.base + (Math.abs(f.exp) === 1 ? '' : sup(Math.abs(f.exp)));
        const withExp = f => f.base + (f.exp === 1 ? '' : sup(f.exp));
        if (den.length === 1 && num.length) return `${num.map(show).join(' ')}/${show(den[0])}`;      // kg/m³, m/s, µmol/L
        return [...num, ...den].map(withExp).join(' ');                                             // m⁻¹ sr⁻¹, µmol quanta m⁻² s⁻¹
    }

    const withUnits = (name, units) => { const u = fmtUnits(units); return u ? `${name} (${u})` : String(name ?? ''); };

    window.fmtUnits = fmtUnits;
    window.withUnits = withUnits;
})();
