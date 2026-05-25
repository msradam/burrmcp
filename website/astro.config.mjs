// @ts-check
import { defineConfig } from 'astro/config';
import starlight from '@astrojs/starlight';
import starlightClientMermaid from '@pasqal-io/starlight-client-mermaid';

// https://astro.build/config
export default defineConfig({
	site: 'https://msradam.github.io',
	base: '/burrmcp',
	integrations: [
		starlight({
			title: 'BurrMCP',
			description: 'Mount Burr state-machine Applications as MCP servers.',
			social: [
				{ icon: 'github', label: 'GitHub', href: 'https://github.com/msradam/burrmcp' },
			],
			plugins: [starlightClientMermaid()],
			sidebar: [
				{ label: 'Home', slug: 'index' },
				{ label: 'Architecture', slug: 'architecture' },
				{ label: 'What works through mount()', slug: 'compatibility' },
				{ label: 'Observability', slug: 'observability' },
				{ label: 'Driving other MCP servers', slug: 'upstream' },
				{ label: 'CLI', slug: 'cli' },
			],
		}),
	],
});
