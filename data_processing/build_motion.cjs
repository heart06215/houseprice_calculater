/* Asset preparation only. The static frontend needs neither Node nor npm. */
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const root = path.resolve(__dirname, '..');
const buildDir = path.join(root, 'output/visual_assets/motion-build');
const assets = path.join(__dirname, 'frontend/assets');
const esbuild = require(path.join(buildDir, 'node_modules/esbuild'));
const packages = ['motion', 'framer-motion', 'motion-dom', 'motion-utils', 'tslib'];
const dependencies = {};
const licenses = packages.map(name => {
    const dir = path.join(buildDir, 'node_modules', name);
    const metadata = JSON.parse(fs.readFileSync(path.join(dir, 'package.json'), 'utf8'));
    dependencies[name] = metadata.version;
    const license = fs.readdirSync(dir).find(file => /^licen[cs]e(?:\.md|\.txt)?$/i.test(file));
    if (!license) throw new Error('Missing license: '+name);
    return name+' '+metadata.version+'\n'+fs.readFileSync(path.join(dir, license), 'utf8');
});
if (dependencies.motion !== '13.4.1' || dependencies['framer-motion'] !== '13.4.1') throw new Error('Unexpected Motion version');
const license = licenses.join('\n\n');
const outfile = path.join(assets, 'vendor/motion-13.4.1.js');
esbuild.buildSync({
    stdin: {contents: 'export { animate as animateMini } from "motion/mini";', resolveDir: buildDir},
    bundle: true, minify: true, format: 'iife', globalName: 'Motion', target: 'es2020', outfile,
    banner: {js: '/*! Motion mini 13.4.1; bundled locally.\n'+license+'\n*/'}
});
fs.writeFileSync(path.join(assets, 'vendor/Motion-LICENSE.md'), license);
const manifestPath = path.join(assets, 'visual-assets.json');
const manifest = JSON.parse(fs.readFileSync(manifestPath, 'utf8'));
manifest.motion_build = {entry: 'motion/mini', format: 'iife', esbuild: require(path.join(buildDir, 'node_modules/esbuild/package.json')).version, dependencies};
for (const name of ['vendor/motion-13.4.1.js', 'vendor/Motion-LICENSE.md']) {
    const bytes = fs.readFileSync(path.join(assets, name));
    manifest.files = manifest.files.filter(file => file.path !== name);
    manifest.files.push({path:name, bytes:bytes.length, sha256:crypto.createHash('sha256').update(bytes).digest('hex')});
}
fs.writeFileSync(manifestPath, JSON.stringify(manifest, null, 2)+'\n');
console.log('Motion mini with licenses:', fs.statSync(outfile).size, 'bytes');
