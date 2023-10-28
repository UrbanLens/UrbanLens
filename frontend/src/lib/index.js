import { start } from '@sveltejs/kit/ssr';

start({
  target: document.getElementById('svelte-app'),
});
