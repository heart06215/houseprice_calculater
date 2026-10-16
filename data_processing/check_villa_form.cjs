'use strict';
/* Offline form-classification probe for HP.RE.community.
   Usage: node data_processing/check_villa_form.cjs <path/to/community.js>
   Prints one JSON object: each value is explain(...)[1].status (the 形态 condition). */
const fs = require('fs');
const vm = require('vm');

const scriptPath = process.argv[2];
if (!scriptPath) {
  console.error('usage: node check_villa_form.cjs <path/to/community.js>');
  process.exit(2);
}

const sandbox = {};
sandbox.window = sandbox;
sandbox.self = sandbox;
vm.runInNewContext(fs.readFileSync(scriptPath, 'utf8'), sandbox);
const P = sandbox.HP.RE.community;

function row(spec) {
  return {
    name: 'x', city: '北京',
    housing_types: spec.housing_types || [],
    residential_forms: spec.residential_forms || [],
    villa_subtypes: spec.villa_subtypes || [],
    type_hints: []
  };
}
function formStatus(spec) {
  return P.explain(row(spec), { forms: ['别墅'] })[1].status;
}

const out = {
  plain_residence_pending: formStatus({ housing_types: ['独栋/联排住宅'] }),
  genuine_villa_supported: formStatus({ housing_types: ['别墅'] }),
  genuine_lianpai_supported: formStatus({ housing_types: ['联排'], villa_subtypes: ['联排'] }),
  genuine_villa_subtype_supported: formStatus({ villa_subtypes: ['叠拼'] }),
  mixed_supported: formStatus({ housing_types: ['高层', '别墅'] })
};

process.stdout.write(JSON.stringify(out) + '\n');
