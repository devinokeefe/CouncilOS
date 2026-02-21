# Final Plan (vFinal)

## Meta
- plan_id: cdc90c47-998f-46e3-a41c-1c1998c6daf4
- candidate_id: B1A
- branch_id: B1
- plan_status: FROZEN
- created_at: 2026-02-21T10:56:12.007156Z
- schema_version: 1.0.0
- source_run_id: a7b21814-a297-463c-9461-d84361c2ddf9

## Project Capsule
### Brief
Create a software to turn my Markdown notes into a searchable static site with tags and backlinks.

### Problem statement
Markdown notes are currently not easily discoverable and navigable; the user needs an automated way to publish notes as a static website with full-text search, tag-based browsing, and backlink navigation between notes.

### Goals
- Ingest a collection of Markdown notes and generate a static website (HTML/CSS/JS) from them.
- Preserve Markdown formatting and structure in the generated site.
- Provide full-text search across generated content in the static site.
- Support tags so users can browse/filter notes by tag.
- Detect and render internal links between notes and generate backlinks ("linked from") sections.
- Produce a build output that can be hosted on common static hosting without requiring a server-side runtime.

### Non-goals
- Building a dynamic, server-rendered web application or requiring a database/server at runtime.
- Real-time collaborative editing or multi-user permissions/roles.
- Authentication or access control features.
- A full Markdown editor; the source of truth remains existing Markdown files.
- Mobile native applications.
- Version control or change tracking beyond what exists in source files.
- Automatic note-taking, summarization, or AI-based content generation.
- Guaranteeing compatibility with every Markdown extension; only a defined subset will be supported.

### Constraints
- CNS1 (tech): Output must be static HTML/CSS/JS that can be hosted on any web server
- CNS2 (tech): Must process standard Markdown syntax as input

### Assumptions
- A1 (impact: high, confidence: medium, needs_confirmation: true): Notes exist as a set of Markdown (.md) files stored as individual files that can be read from a local directory and/or repository.
- A2 (impact: high, confidence: low, needs_confirmation: true): Internal links between notes can be identified via a consistent convention (e.g., Markdown links to filenames/paths or wiki-style links like [[Note Title]]).
- A3 (impact: high, confidence: low, needs_confirmation: true): Tags can be derived from a consistent convention (e.g., YAML front matter tags and/or inline #tags) and do not require manual curation in the generated site.
- A4 (impact: high, confidence: medium, needs_confirmation: true): Search will be implemented client-side (e.g., a prebuilt index shipped with the site) to keep the output fully static.
- A5 (impact: medium, confidence: medium, needs_confirmation: true): A command-line build workflow is acceptable (run a build command to generate the site).
- A6 (impact: low, confidence: medium, needs_confirmation: false): The site will be used by a single user or small group without access control needs.

### Open questions
- Q1 (blocking: true, impact: high): What Markdown/linking conventions must be supported for internal links and backlinks (standard Markdown links, relative paths, wiki-links [[...]], Obsidian-style links, etc.)?
- Q2 (blocking: true, impact: high): How should tags be specified and parsed (YAML front matter, inline hashtags, a dedicated syntax), and what are the rules for tag normalization (case, spaces, nesting)?
- Q3 (blocking: true, impact: high): What scale is required (approx. number of notes and total size), to choose an indexing/search approach and performance targets?
- Q4 (blocking: true, impact: high): What is the desired information architecture: required pages and navigation (home, all notes, tags index, per-tag pages, per-note page layout)?
- Q5 (blocking: false, impact: medium): What hosting/deployment target is expected (local file browsing, GitHub Pages, Netlify, other), and are there constraints on relative paths/base URLs?
- Q6 (blocking: false, impact: medium): What Markdown flavor/features must be supported (e.g., CommonMark/GFM; tables, footnotes, math/LaTeX, mermaid/diagrams, code highlighting), and is there an existing renderer preference?
- Q7 (blocking: false, impact: medium): Should the build be incremental (only rebuild changed notes) and/or provide a watch mode for local preview?
- Q8 (blocking: false, impact: medium): Should the site expose outgoing links, backlinks only, or a full graph view; and how should broken links be handled (warn, fail build, render as plain text)?
- Q9 (blocking: false, impact: low): Are there specific design or theming requirements for the generated site?
- Q10 (blocking: true, impact: medium): Should the tool run as a CLI, GUI application, or build script integration?

### Success metrics
- Given an input directory of Markdown notes, the tool generates a static site output directory successfully (build completes without errors on a supported environment).
- All Markdown files in the input set are rendered into accessible HTML pages in the output (excluding explicitly configured ignores).
- Full-text search returns relevant results for queries that match note titles and body text, with search interaction remaining responsive at the confirmed target note scale.
- For any valid internal link between notes, the destination note page includes a backlinks section listing the source note after a successful build, and backlinks are rendered as clickable links.
- Tag index pages are generated such that selecting a tag shows all notes containing that tag, and each note page displays its associated tags.
- Generated site loads and functions in modern browsers without errors.

### Stakeholders
- Primary user (note owner/author)
- Readers/consumers of the published notes (if shared)

### Glossary
- Static site: A website consisting of pre-generated files (e.g., HTML/CSS/JS) served without server-side rendering or a runtime database.
- Markdown: A lightweight markup language using plain text formatting syntax that converts to HTML.
- Tag: A label associated with a note, used to group and browse notes by topic.
- Backlink: A reverse reference showing which other notes link to the current note.
- Full-text search: Search that matches terms in the content of notes (not only titles/metadata), typically via a prebuilt index for static sites.

## Executive summary
Select O1 (monolithic single-process CLI pipeline) to deliver a static HTML/CSS/JS site generated from a directory of Markdown notes. Execute a thin-slice plan that produces a deployable site early (Milestone 1: ingest + render), then incrementally adds tags (M2), internal link resolution + backlinks (M3), client-side search with a build-time index (M4), and final compatibility/polish items including broken-link warnings, base URL support, GFM extensions, and cross-browser QA (M5). This approach optimizes for simplicity and rapid time-to-value while explicitly managing convention ambiguity risks for links/tags via required configuration before implementing those features.

## Requirements
### MUST
- R1: The system MUST accept a user-specified local directory path as input and recursively discover Markdown files, including all files with the ".md" extension by default.
- R2: The system MUST generate a static build output consisting only of HTML, CSS, JavaScript, and other static assets that can be hosted/served without any server-side runtime or database dependency.
- R3: The system MUST parse each discovered Markdown file using at least CommonMark-compliant syntax and generate a corresponding HTML page for every input Markdown file not explicitly excluded by configuration.
- R4: The system MUST preserve standard Markdown formatting and structure in generated HTML (including headings, lists, emphasis, links, images, and code blocks).
- R5: The system MUST provide a command-line interface (CLI) build workflow that accepts at minimum an input directory path and an output directory path and executes the full build pipeline to generate the site output directory.
- R6: The build process MUST either complete successfully for a valid input set or report clear, actionable error messages on failure.
- R7: The generated site MUST include client-side full-text search across note titles and body content that executes entirely in the browser without requiring server-side processing or server requests to perform searches.
- R8: The build process MUST generate a full-text search index artifact at build time and include/ship it with the static site output for use by the client-side search feature.
- R9: The system MUST detect internal links between notes and, at minimum, correctly resolve standard Markdown relative links to other Markdown notes in the input set into navigable links to the corresponding generated HTML pages.
- R10: For each note that is linked to by other notes, the generated note page MUST include a backlinks section listing all notes that link to it, and each backlink entry MUST link to the source note page.
- R11: The system MUST extract tags from each note using at least one supported tagging convention that is user-selectable/configurable (e.g., YAML front matter tags field and/or inline "#tag" syntax) and associate the extracted tags with the note in the generated site.
- R12: Each generated note page MUST display its associated tags and render each tag as a navigable link to the corresponding tag page.
- R13: The generated site MUST include a tags index page and per-tag pages that list all notes associated with each tag.
- R14: The generated site MUST load and function correctly in the current stable releases of Chrome, Firefox, Safari, and Edge without JavaScript errors that block core functionality (navigation, search, backlinks, tags).

### SHOULD
- R15: The system SHOULD provide configurable rules for internal link parsing and support additional link syntaxes beyond standard Markdown relative links (e.g., wiki-style links like "[[Note Title]]"), as selected by the user.
- R16: The system SHOULD provide configurable tag normalization rules (e.g., lowercasing and trimming whitespace) applied consistently across tag pages and note tag displays.
- R17: The generated site SHOULD include a home/landing page and a navigation structure that provides access to an all-notes listing, the tags index, and the search feature, and these navigation entry points SHOULD be reachable from any note page.
- R18: The system SHOULD emit build-time warnings for unresolved internal links (including source file and link text), SHOULD not fail the build by default for such links, and SHOULD render unresolved links in a clearly non-navigable manner in the output.
- R19: The system SHOULD allow configuration of a base URL and/or path prefix so the generated site can be deployed under a subdirectory (e.g., GitHub Pages project sites).
- R20: The system SHOULD allow users to exclude specific files or directories from the build via a configuration file or CLI option.
- R21: The system SHOULD support GitHub Flavored Markdown (GFM) extensions including tables, strikethrough, task lists, and fenced code blocks with syntax highlighting.
- R22: For collections of up to 1,000 notes on a modern consumer device, full-text search results SHOULD be returned and rendered in under 500 ms.

### COULD
- R23: The system COULD support incremental builds that only regenerate pages for notes that have changed since the last build.
- R24: The system COULD provide a local development server and live-reload/watch mode that automatically rebuilds and refreshes the browser when source files change.
- R25: The generated site COULD include an interactive graph visualization showing link relationships between notes.
- R26: The system COULD support user-selectable themes and/or custom CSS injection for visual customization of the generated site.
- R27: The generated site COULD render outgoing links from each note as a dedicated section alongside the backlinks section.

## Acceptance tests
- AT1 → R1 (system)
  - Procedure: Prepare nested directory with a.md, sub/b.md, sub/deeper/c.md, and sub/d.txt. Run CLI build.
  - Pass: Discovers exactly the three .md files; excludes d.txt.
- AT2 → R2 (system)
  - Procedure: Build fixture; inspect output file types; serve statically and open.
  - Pass: Only static assets emitted; site loads via static serving.
- AT3 → R3 (system)
  - Procedure: Fixture with N Markdown files and config excluding one; build; count pages.
  - Pass: One HTML per non-excluded Markdown; excluded has no page.
- AT4 → R4 (system)
  - Procedure: Fixture note with headings/lists/emphasis/links/images/fenced code blocks; build; inspect render.
  - Pass: Correct HTML structure (<h1>/<h2>, <ol>/<ul>, <em>/<strong>, <a>, <img>, <pre><code>).
- AT5 → R5 (system)
  - Procedure: Invoke CLI with input and output args; record exit code; check output.
  - Pass: Exit code 0; output directory created with generated files.
- AT6 → R6 (system)
  - Procedure: Case A valid fixture; Case B invalid input path or malformed config; capture messages.
  - Pass: Case A exit 0; Case B non-zero and actionable error.
- AT7 → R7 (system)
  - Procedure: Build fixture with known terms; open site with network disabled; search title-only and body-only terms.
  - Pass: Results correct, in-browser only, links navigate to notes.
- AT8 → R8 (system)
  - Procedure: Build; verify search index artifact in output; verify UI loads it.
  - Pass: Index generated at build time and loaded from static output.
- AT9 → R9 (system)
  - Procedure: note-a links to note-b.md; build; click link.
  - Pass: Navigates to generated HTML for note-b.
- AT10 → R10 (system)
  - Procedure: note-a and note-b link to note-c; build; open note-c; inspect backlinks; click each.
  - Pass: Backlinks section lists note-a and note-b; both clickable.
- AT11 → R11 (system)
  - Procedure: Configure tag convention (e.g., YAML tags or inline #tag); create tagged notes; build; inspect rendered metadata.
  - Pass: Tags extracted per configured convention and associated correctly.
- AT12 → R12 (system)
  - Procedure: Build with at least two tags; open note; click tag links.
  - Pass: Tags displayed and navigate to tag pages.
- AT13 → R13 (system)
  - Procedure: Build; locate tags index and per-tag page(s); verify listings.
  - Pass: Tags index exists; per-tag pages list all and only notes with that tag.
- AT14 → R14 (system)
  - Procedure: Open built site in latest stable Chrome/Firefox/Safari/Edge; navigate notes; search; open tag page; monitor console.
  - Pass: Core functionality works in each browser without blocking JS errors.

## Architecture
### Options
- O1: Custom-built monolithic CLI pipeline (single-process) that scans Markdown files, extracts metadata/tags/links, builds a note graph with backlinks, renders HTML via templates, generates a client-side search index, and writes a fully static HTML/CSS/JS output (implementation can be Node.js- or Python-based).
- O2: Plugin-based build core with defined lifecycle hooks (ingest/transform/render/emit) and a shared build context or data store; features implemented as swappable plugins.
- O3: Build on an existing static site generator (e.g., Eleventy/11ty) and add custom configuration/plugins for wiki-links/backlinks, tag pages, and a search index.
- O4: Multi-stage build pipeline with intermediate artifacts (e.g., JSON per note plus a global graph manifest) enabling caching and incremental rebuilds.

### Chosen
- option_id: O1
- Rationale: O1 is the lowest-complexity architecture that directly satisfies the core MUST requirements (directory ingestion, per-note rendering to static assets, backlinks, tags, and client-side search with a build-time index) without introducing plugin API surface area or external SSG coupling. Given the scope and target scale (up to ~1,000 notes as a SHOULD target), a single-process in-memory graph and straightforward pipeline is appropriate; later extensibility can be revisited if additional workflows (incremental builds, watch mode, plugin ecosystem) become priorities.

### O1 components
- C1: CLI Orchestrator
- C2: Source Scanner & Front Matter Parser
- C3: Content Analyzer (Links/Tags) & Graph Builder
- C4: Markdown Renderer
- C5: Template & Theme Renderer
- C6: Search Index Builder
- C7: Output Emitter & Asset Copier
- C8: Client-Side Runtime (Search UI)

### O1 interfaces
- IF1: C1 → C2: Pass validated build configuration.
- IF2: C2 → C3: Provide NoteFile[] ({slug/id, rawMarkdown, frontMatter, filePath}).
- IF3: C3 → C4: Provide NoteGraph with per-note ({rawMarkdown, title, tags, outLinks, backLinks}).
- IF4: C4 → C5: Provide RenderedNote[] ({slug, title, htmlContent, tags, backLinks}).
- IF5: C3 → C6: Provide data needed to build the search index.
- IF6: C5 → C7: Provide PageOutput[] ({relativePath, htmlString}).
- IF7: C6 → C7: Provide serialized search index assets.
- IF8: C7 → C8: Browser fetches search index assets from static output.

### O1 tradeoffs
- Pros
  - Straightforward end-to-end build flow and debugging (single CLI program)
  - No server runtime required; outputs static HTML/CSS/JS
  - Easy to package as a single tool users run against a notes directory
  - Good performance for small-to-medium note sets with in-memory processing
- Cons
  - Harder to extend safely compared to a formal plugin architecture
  - Potential scalability limits (memory/time) on very large note sets without caching/incremental rebuilds
  - Implementation stack choice (Node vs Python) affects ecosystem fit, performance, and distribution approach
- Risks
  - Large collections can lead to high memory usage if all notes and derived structures are held in memory
  - Client-side search index size can grow large and impact browser load/interaction time
  - Tight coupling between graph-building and rendering may complicate future extensibility

## Milestones
### M1 — Intake→Generate: CLI skeleton, Markdown ingestion, per-note HTML rendering, and static output emission
Deliverables
- DL1: CLI command `build` accepting input/output dirs; validates; actionable errors (R5, R6).
- DL2: Recursive scanner for .md with deterministic sorted file list (R1).
- DL3: CommonMark parsing/rendering into one HTML page per non-excluded note (R3, R4).
- DL4: Basic template/layout and asset emission; purely static output (R2).
- DL5: Minimal navigation surface (e.g., all-notes listing) for early slices (supports R17 as early optional enhancement).
- DL6: Malformed front matter handling: warn and continue where feasible (addresses K6).
Exit criteria
- AT1–AT6 pass on a fixture dataset.
- Output is static and loads when served statically (AT2).
- One-to-one mapping between non-excluded Markdown and generated HTML is verified (AT3).
Depends on: (none)

### M2 — Generate: Tag extraction + tags index + per-tag pages + tag links on notes
Deliverables
- DL7: Configurable tag extraction supporting at least one selected convention (R11).
- DL8: Optional configurable tag normalization (case/trim) (R16).
- DL9: Tags displayed on note pages and linked to tag pages (R12).
- DL10: Tags index and per-tag pages listing associated notes (R13).
Exit criteria
- AT11–AT13 pass for chosen tagging configuration.
- Deterministic and documented extraction/normalization behavior (mitigates K3).
Depends on: M1

### M3 — Generate: Internal link resolution to generated HTML + backlinks section on notes
Deliverables
- DL11: Resolver maps standard Markdown relative links to generated HTML (R9).
- DL12: Backlink graph computed; note pages include backlinks section with clickable links (R10).
- DL13: Canonical note identifier + output path strategy; prevent collisions with defined precedence (mitigates K10/K9).
Exit criteria
- AT9–AT10 pass on fixtures including multiple backlinks to a single target.
- Backlinks handle cycles without recursion failures and stable ordering (mitigates K9/K20).
Depends on: M1

### M4 — Generate: Client-side full-text search with build-time index artifact + search UI
Deliverables
- DL14: Build-time search index artifact from titles/body; emitted with site (R8).
- DL15: Client-side search UI loads shipped index; in-browser search (R7).
Exit criteria
- AT7–AT8 pass with network disabled (or no outbound requests).
- Search responsiveness checked on representative datasets; capture timing/size metrics toward R22.
Depends on: M1

### M5 — Validate→Review: Polish, compatibility, and risk mitigations (broken links, base URL, GFM, browser QA, sanitization defaults)
Deliverables
- DL16: Build-time warnings for unresolved internal links; non-fatal by default; non-navigable rendering (R18).
- DL17: Base URL / path prefix configuration for subdirectory hosting (R19).
- DL18: GFM extension support plan + implementation for tables/strikethrough/task lists; syntax highlighting strategy (R21).
- DL19: Cross-browser QA runbook and results for Chrome/Firefox/Safari/Edge (R14).
- DL20: Security defaults: raw HTML disabled or sanitized by default; template escaping documented (mitigates K7).
- DL21: Privacy exposure documentation banner/README disclaimer (K8 acceptance posture).
Exit criteria
- AT11–AT14 run end-to-end; AT14 passes across all target browsers.
- Broken-link warnings verified; do not fail build by default (R18).
- Base path deployment smoke test performed for subdirectory hosting (R19/K13).
Depends on: M2, M3, M4

## Risk register
- K1 (high): Incorrect/unsupported Markdown flavor parsing/rendering.
  - Mitigation (planned, owner: eng): Select/document supported flavor; golden-file tests; compatibility matrix; warnings for unsupported constructs.
- K2 (high): Ambiguous/unsupported internal link conventions.
  - Mitigation (planned, owner: eng): Explicit link-convention config; deterministic resolver; strictness; fixture tests.
- K3 (high): Tag extraction/normalization undefined/inconsistent.
  - Mitigation (planned, owner: eng): Single normalization pipeline; configurable sources/precedence; validation report.
- K4 (critical): Build too slow/crashes/hangs at required scale.
  - Mitigation (planned, owner: eng): Stream where feasible; cap caches; progress logging; stress tests; hardware recommendations.
- K5 (high): Client-side search index size/performance scales poorly.
  - Mitigation (planned, owner: eng): Confirm scale (Q3); choose appropriate indexing; chunk/lazy load; budgets and size report.
- K6 (medium): Malformed YAML front matter fails build.
- K7 (high): XSS/content injection risk if content not sanitized/escaped.
  - Mitigation (planned, owner: sec): Disallow/sanitize raw HTML; escape templates; security tests; document trust model.
- K8 (critical): Privacy leakage by publishing sensitive notes.
- K9 (medium): Backlink generation incorrect/incomplete; cycles/edge cases.
  - Mitigation (planned, owner: eng): Canonical path IDs; explicit link graph; consistency checks; golden tests.
- K10 (medium): Slug/title collisions overwrite pages or break resolution.
  - Mitigation (planned, owner: eng): Canonical path IDs; explicit link graph; consistency checks.
- K11 (high): Cross-platform/path/encoding issues.
  - Mitigation (planned, owner: eng): Normalize paths; define encodings; cross-platform tests; clear errors; skip option.
- K12 (medium): Broken internal links handled inconsistently.
  - Mitigation (planned, owner: eng): Link validation; strict/warn modes; broken links report; optional CI fail.
- K13 (medium): Base URL/relative path issues on hosting targets.
  - Mitigation (planned, owner: eng): Configurable basePath/baseUrl; consistent URLs; deploy test matrix; link checker.
- K14 (medium): Incremental/watch mode stale/inconsistent output.
  - Mitigation (planned, owner: eng): Optional feature; invariants vs clean build; clean command; corruption detection.
- K15 (medium): Accessibility issues.
  - Mitigation (planned, owner: eng): Accessible base theme; automated checks; document assumptions.
- K16 (low): Poor mobile responsiveness.
- K17 (medium): IA/navigation mismatches user expectations.
  - Mitigation (planned, owner: pm): Resolve Q4; minimal default nav; configurable menus; sitemap page; breadcrumbs.
- K18 (medium): Search relevance poor.
  - Mitigation (planned, owner: eng): Relevance tests; configurable tokenization/stopwords; title/tag boosting.
- K19 (high): Theme customization introduces unsafe injections.
  - Mitigation (planned, owner: sec): Limit customization to safe config; sandbox/lint templates if supported; document boundaries; disable arbitrary JS by default.
- K20 (medium): Non-deterministic builds.
  - Mitigation (planned, owner: eng): Deterministic ordering; avoid timestamps; control locale/timezone; CI check.
- K21 (medium): Dependency supply-chain issues.
  - Mitigation (planned, owner: sec): Pin deps; vuln scanning; upgrade cadence; prefer well-maintained libs.
- K22 (low): Insufficient error messages/logging.
  - Mitigation (planned, owner: eng): Structured logs; summaries; file/line context; final build report.
- K23 (low): CLI installation/UX barriers.

## Governance
### Tool policy (by stage)
- Intake: allowlisted_tools = artifact_read, artifact_write; no external network; write scope restricted.
- Generate: allowlisted_tools = artifact_read; no external network; no side effects; must trace to confirmed inputs.
- Validate: allowlisted_tools = artifact_read; no external network; consistency checks required.
- Review: allowlisted_tools = artifact_read, artifact_write; no external network; write scope restricted; require citations.
- Judge: allowlisted_tools = artifact_read; no external network; no writes; explainable basis.
- Freeze: allowlisted_tools = artifact_read; no external network; no writes; no new requirements.

### HITL policy: when to interrupt
- Unconfirmed blocker questions (Q1/Q2/Q3/Q4/Q10) would materially change architecture or success metrics.
- Any decision affecting security boundaries (raw HTML, template execution, third-party JS/search libraries).
- Any proposal that violates hard constraints (static-only output) or non-goals.
- Any need to accept a high/critical risk without a mitigation plan.
- Any build process failure.
- Validation detects high broken links/accessibility violations above threshold.
- Build time or memory exceeds performance targets for specified scale.

Approval roles
- Product Owner
- Security Reviewer
- Tech Lead
- Developer
- Project Lead

## Decision log
- DEC1: Implement O1 monolithic CLI pipeline for initial release.
- DEC2: Canonical note identifier/output paths: use source-relative file paths (normalized) + deterministic mapping; detect collisions.
- DEC3: Link syntax support: MVP supports standard Markdown relative links (R9); additional syntaxes (e.g., wiki-links) only via explicit config (R15); unresolved links warn (R18) and render non-navigable.
- DEC4: Tags: configurable extraction (front matter and/or inline hashtags) with optional normalization; validation summary of tag counts.
- DEC5: Search: build-time JSON corpus/index shipped with site; client-side JS loads it and performs in-browser full-text search.
- DEC6: Security stance: disallow raw HTML or sanitize; templates escape user-controlled strings by default.
- DEC7: Unresolved links: build-time warnings; do not fail by default; render clearly non-navigable (optionally report).
- DEC8: Subpath deployment: configurable baseUrl/basePath applied consistently during emission; include smoke test.

## Supplemental evaluations
- Acceptance test sequencing aligned to thin-slice milestones
  - Notes: Run AT1–AT6 after M1; AT11–AT13 after M2; AT9–AT10 after M3; AT7–AT8 after M4; AT14 and end-to-end after M5.

## Synthesis warnings
- W1 (high): Q1 and Q2 are blocking and materially affect M2–M3 scope/resolver behavior; proceed with M1 but require confirmation/config decisions before implementing tags/links/backlinks beyond the minimum.
- W2 (critical): K8 (privacy leakage) is critical and currently planned for documentation-only mitigation; confirm user intent and acceptable disclosure posture before Freeze.

## Appendix
### Glossary
- Static site: A website consisting of pre-generated files (e.g., HTML/CSS/JS) served without server-side rendering or a runtime database.
- Markdown: A lightweight markup language using plain text formatting syntax that converts to HTML.
- Tag: A label associated with a note, used to group and browse notes by topic.
- Backlink: A reverse reference showing which other notes link to the current note.
- Full-text search: Search that matches terms in the content of notes (not only titles/metadata), typically via a prebuilt index for static sites.
