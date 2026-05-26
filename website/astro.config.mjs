// @ts-check
import { defineConfig } from 'astro/config';
import starlight from '@astrojs/starlight';
import starlightClientMermaid from '@pasqal-io/starlight-client-mermaid';
import starlightThemeRosePine from 'starlight-theme-rose-pine';

// https://astro.build/config
export default defineConfig({
	site: 'https://msradam.github.io',
	base: '/theodosia',
	integrations: [
		starlight({
			title: 'Theodosia',
			description: 'Mount Burr state-machine Applications as MCP servers.',
			customCss: ['./src/styles/theodosia.css', './src/styles/theodosia-overrides.css'],
			social: [
				{ icon: 'github', label: 'GitHub', href: 'https://github.com/msradam/theodosia' },
			],
			plugins: [
				starlightThemeRosePine({
					dark: { flavor: 'main', accent: 'iris' },
					light: { flavor: 'dawn', accent: 'iris' },
				}),
				starlightClientMermaid(),
			],
			sidebar: [
				{
					label: 'Start',
					items: [
						{ label: 'Authoring a graph', slug: 'authoring' },
						{ label: 'Examples', slug: 'examples' },
					],
				},
				{
					label: 'Concepts',
					items: [
						{ label: 'Architecture', slug: 'architecture' },
						{ label: 'Refusals and recovery', slug: 'refusals' },
						{ label: 'Sessions and forking', slug: 'sessions' },
						{ label: 'Security model', slug: 'security-model' },
						{ label: 'Research foundation', slug: 'research-foundation' },
					],
				},
				{
					label: 'Reference',
					items: [
						{ label: 'MCP tools and resources', slug: 'tools' },
						{ label: 'CLI', slug: 'cli' },
					],
				},
				{
					label: 'Integration',
					items: [
						{ label: 'What works through mount()', slug: 'compatibility' },
						{ label: 'Driving other MCP servers', slug: 'upstream' },
						{ label: 'Observability', slug: 'observability' },
					],
				},
			],
		}),
	],
});
