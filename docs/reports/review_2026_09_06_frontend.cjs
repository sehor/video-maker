// Review-only probes. Executes current Vue script logic with fake dependencies;
// no browser, network, database, or real generation requests are used.
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.resolve(__dirname, '../..');
const ts = require(path.join(root, 'apps/web/node_modules/typescript'));

function loadScript(file, exposed, dependencies) {
  const source = fs.readFileSync(path.join(root, file), 'utf8')
    .match(/<script setup lang="ts">([\s\S]*?)<\/script>/)[1];
  const output = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText;
  const context = vm.createContext({
    exports: {}, ref: value => ({ value }),
    useRoute: () => ({ params: { id: 'project', shot_id: 'shot' } }),
    onMounted: () => {}, onBeforeUnmount: () => {},
    ...dependencies,
  });
  vm.runInContext(output + `\nglobalThis.reviewEntry = ${exposed};`, context);
  return context.reviewEntry;
}

(async () => {
  let quotes = 0;
  let acceptedJobs = 0;
  const generate = loadScript(
    'apps/web/app/pages/projects/[id]/shots/[shot_id].vue', 'generate', {
      navigateTo: async () => {},
      useApi: () => ({ request: async endpoint => {
        if (endpoint === '/v1/quotes') return { id: `quote-${++quotes}` };
        if (endpoint === '/v1/generations') {
          acceptedJobs++;
          if (acceptedJobs === 1) throw new Error('Response lost AFTER server accepted the Job');
          return { id: `job-${acceptedJobs}` };
        }
        throw new Error(`Unexpected endpoint: ${endpoint}`);
      }}),
    },
  );
  await generate();
  await generate();
  console.log(JSON.stringify({ probe: 'resubmit_after_lost_response', expectedJobs: 1, acceptedJobs, quotes }));

  let scheduledPolls = 0;
  const load = loadScript('apps/web/app/pages/jobs/[id].vue', 'load', {
    useApi: () => ({ request: async () => { throw new Error('Temporary network failure'); } }),
    setTimeout: () => { scheduledPolls++; },
  });
  await load().catch(() => {});
  console.log(JSON.stringify({ probe: 'poll_after_transient_failure', expectedScheduledPolls: 1, scheduledPolls }));
  process.exitCode = acceptedJobs === 1 && scheduledPolls === 1 ? 0 : 1;
})().catch(error => { console.error(error); process.exitCode = 2; });
