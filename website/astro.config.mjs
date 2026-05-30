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
			title: '⊢ Theodosia',
			description: 'Mount Burr state-machine Applications as MCP servers.',
			customCss: ['./src/styles/theodosia.css', './src/styles/theodosia-overrides.css'],
			components: {
				Header: './src/components/DocsHeader.astro',
				Sidebar: './src/components/DocsSidebar.astro',
				Banner: './src/components/DocsBanner.astro',
				ContentPanel: './src/components/DocsContentHeader.astro',
			},
			social: [
				{ icon: 'github', label: 'GitHub', href: 'https://github.com/msradam/theodosia' },
			],
			plugins: [
				starlightThemeRosePine({
					dark: { flavor: 'main', accent: 'foam' },
					light: { flavor: 'dawn', accent: 'foam' },
				}),
				starlightClientMermaid(),
			],
			sidebar: [
				{
					label: 'Start',
					items: [
						{ label: 'Introduction', slug: 'introduction' },
						{ label: 'Build your own agent', slug: 'tutorial' },
						{ label: 'Authoring a graph', slug: 'authoring' },
						{ label: 'Deployment recipes', slug: 'deployment' },						{ label: 'Examples', slug: 'examples' },
					],
				},
				{
					label: 'Concepts',
					items: [
						{ label: 'Refusals and recovery', slug: 'refusals' },
						{ label: 'Sessions and forking', slug: 'sessions' },
						{ label: 'Architecture', slug: 'architecture' },
						{ label: 'Personas', slug: 'personas' },
						{ label: 'Security model', slug: 'security-model' },
					],
				},
				{
					label: 'Evidence',
					items: [
						{ label: 'Case study', slug: 'case-study' },
						{ label: 'Research foundation', slug: 'research-foundation' },
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
				{
					label: 'Reference',
					items: [
						{ label: 'MCP tools and resources', slug: 'tools' },
						{ label: 'CLI', slug: 'cli' },
					],
				},
			],
		}),
	],
});
