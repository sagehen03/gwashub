# Where GWASHub stands

Current state, decisions and known gaps (2026-10-10). The site is feature-complete for now and validated; what's left
is the production items. How things changed day by day is in the git history; how to run it is in `README.md`, the AWS
deployment in `docs/AWS_FULL.md`.

## Built

| Area | State |
|---|---|
| Website (Nuxt 4 + PrimeVue 4) | Every page on the API: projects, overview, datasets, cohorts, upload wizard, dataset panel (QC and Details), pipeline settings, meta-analysis builder and results, Account (bulk upload, API tokens), Validation, About; six guided tours in the Help menu |
| API (FastAPI) | Server-side permissions; uploads in parts; pending QC; QC jobs; reviews; named, versioned pipelines; resources; cohorts and study metadata; meta-analysis; dataset and project deletion; bulk upload (Excel manifest, check, API tokens); the Practice project |
| Default QC | The HERMES 3 DCM pipeline as eight steps (below). Tested on synthetic data and dry runs on real files; not yet compared with the HPC results on real cohorts |
| Runner | One process per step, in the step's image; resources per step; block / flag / continue; crash handling; Python and R steps; image groups as Batch jobs |
| Meta-analysis | `gwashub/gwama` (default), `gwashub/metal`, consortium methods by PR; shared post-processing: lead loci by PLINK 2 clumping, signals, locus plots with genes, forest plots, `datasets.tsv`, `README.txt` |
| Reference files | `resources/catalog.yaml` and build scripts; all 37 real files (~26 GB, incl. dbSNP 157) on AWS dev |
| AWS | Live in gwashub-dev (`docs/AWS_FULL.md`); prod configured, not deployed. Can hibernate (near-zero cost, data kept): `devtools/aws_hibernate.sh`, not yet used |
| Shared host (Broad) | A second deployment target, built and tested but not deployed (`docs/SHARED_HOST.md`): GWASHub on the Broad's existing EC2 host, a new database on their MySQL server and their nginx proxy, with a cut-down stack `Gwashub-<env>-Shared` (S3, KMS, Cognito, Batch, SQS; about $5 a month fixed). MySQL 8.0 and 8.4 are supported alongside Postgres and SQLite (migrations, UTC timestamps, reconnects after an idle drop; the API tests run on MySQL 8.0, 8.4 and Postgres 16 with `devtools/test_db.sh`). `devtools/shared_host_setup.sh` and `shared_host_deploy.sh` (with `rollback`, which refuses when the live release added a migration), each checking first that the SSH target is the configured host (its instance ID, from IMDSv2), and `shared_host_admin.sh`; `devtools/shared_host.py` renders the env file, units and nginx block and checks the site (it must say its sign-in is Cognito's, and the dev sign-in must not answer). The services' wrapper reads the env file as `KEY='value'` lines and never runs it; the three secrets deny the host's role in their own policies; a MySQL database without a case-sensitive collation is refused. Reference files go up with `devtools/aws_upload_resources.py` |
| Validation | All four tests pass on gwashub-dev: GWAMA defaults and options and METAL, each vs the program run by hand, and QC against planted problems (`docs/VALIDATION_PLAN.md`, `validation/`) |
| Tests | 268 in `.venv/bin/python -m pytest` (2026-11-23: 197 passed; 47 skipped for want of the step images from `./devtools/build_images.sh` and 6 for want of a MySQL test database, which `devtools/test_db.sh` provides; the 18 that run QC need R 4.4+ and failed there, as that machine has R 4.1.2). The API tests also run on MySQL 8.0, 8.4 and Postgres 16 with `devtools/test_db.sh`, and the CDK stacks have 25 more (`infra/.venv/bin/python -m pytest infra/tests`) |

## Default QC pipeline

From `nicksunderland/hermes3_dcm` (commit ff35109), split into steps. Every step after `parse` reads and writes the
GWASHub format (`_lib/common.py` `STANDARD`: `chr pos ea oa beta se p eaf` + optional `n ncase ncontrol rsid info`).

| Step | Image | What it does |
|---|---|---|
| `parse` (R) | `gwashub/base-r` | The only step that reads the upload. Recodes and validates; removes rows it can't recover (missing or invalid essentials, EAF missing, 0 or 1); P from BETA/SE only where missing; N from cases + controls. Reports missingness and out-of-bounds counts per column, chromosomes present, indels. Removed rows as uploaded, with line and reasons. Upload gotchas below |
| `filter` (R) | `gwashub/base-r` | Chromosome set; range filters (\|BETA\|, SE, INFO, MAF, N), each with its own switch and min/max switches; missing INFO filled with 1 or removed; strand-ambiguous (A/T, C/G) SNPs by MAF (off by default); indels; duplicates. Warns above 5% removed. Removed rows with the filter that removed each |
| `harmonise_liftover` | `gwashub/vcf` (x86-64) | The edited gwas2vcf (copied into the step) and bcftools +liftover; dbSNP 157 rsIDs. GRCh38 with EA = ALT afterwards. Removed rows with stage and reason (gwas2vcf's from a `--rejected` list we added to our copy; liftover's from bcftools' reject file). Checks the declared build first (below) |
| `eaf_check` | `gwashub/vcf` | EAF vs 1000 Genomes; warns above 5% with \|ΔEAF\| > 0.2; removes them only when `remove_large_diff` is on (default off). Names MAF given as EAF, and strand problems in A/T, C/G SNPs |
| `ldsc` | `gwashub/ldsc` (x86-64) | The authors' bulik/ldsc (Python 2.7), Pan-UKB LD scores; intercept and ratio |
| `stats_summary`, `plots`, `qc_report` | `gwashub/base` | λ metrics, hits, lead loci by distance, SE vs sample size, BETA/SE vs P; Manhattan and QQ; the HTML report |

**Common GWAS QC gotchas** each have a check that names the problem (rather than letting it show up as a vague
drop in rows or an odd λ):

| Gotcha | Where | Outcome |
|---|---|---|
| gzip upload cut short | `parse` (`gzip -t`) | fail |
| Odds ratios uploaded as BETA (all positive, median near 1); log-odds uploaded as OR (values ≤ 0) | `parse` | fail |
| P column holding −log10 P | `parse` | fail (existing) |
| Header lines inside the data (files joined together) | `parse` | removed, reason "repeated header line" |
| Alleles coded 1/2 or I/D | `parse` | named in the messages (rows removed as invalid) |
| PLINK chromosome 25 / XY | `parse` | read as X |
| BETA, SE or P rounded (fewer than 1,000 distinct values in 50,000+ rows) | `parse` | warn |
| N above the study's total | `parse` | warn |
| An autosome missing from a genome-wide file (50,000+ rows) | `parse` | warn |
| EAF never above 0.5 | `parse` | note (minor-allele coding or MAF; `eaf_check` tells which) |
| Wrong build declared | `harmonise_liftover`: 20,000 SNPs looked up in both FASTAs | warn, naming the build that fits |
| MAF uploaded as EAF | `eaf_check`: agrees with 1000 Genomes only once folded to MAF | named |
| A/T, C/G SNPs on the other strand | `eaf_check`: those SNPs differ far more often than the rest | warn; `filter` can remove them by MAF |
| Effects on the linear 0/1 scale (BOLT-LMM, linear regression) or N wrong | `stats_summary`: median SE / SE expected from cases, controls, EAF (EasyQC's SE-N check), 0.4 to 2.5 | warn, naming which |
| SE on another scale (e.g. of the OR) | `stats_summary`: median \|BETA/SE\| / Z from P outside 0.8 to 1.25 | warn |
| Wrong ancestry declared | `eaf_check`: frequencies clearly closer to another 1000 Genomes population | warn, naming it |
| Very rare variants in small cohorts | `filter`: minor allele count 2·MAF·N (EasyQC's cut-off 6) | removed when switched on (off by default) |
| Datasets sharing samples, or the same data twice | meta-analysis post-processing: Z correlation across null variants (\|Z\| < 2) above 0.1 (0.98: same data) | warn; pairs in summary.json |

Checklists compared: EasyQC (Winkler et al. 2014, Nat Protoc 9:1192) and MungeSumstats. Not covered: X-chromosome
coding in men (0/1 vs 0/2) and Hardy-Weinberg / call-rate checks (need genotype-level fields uploads don't carry).

The runner profiles every run's upload and cleaned file (`_lib/profile.py`) and counts rows before and after each step,
whatever the pipeline. A QC run is four Batch jobs (one per change of image).

## Decisions

**Data and QC**
- Steps follow one contract (`--input --resources --params --log`, JSON step log with a verdict) and never touch the
  database or the network. They declare the resources they need; only those are staged.
- Steps that remove rows list them in `removed_rows.tsv.gz` with a `reason` (`docs/QC_STEP_OUTPUT_PLAN.md`). The dataset
  panel shows each step as one line (title, verdict); expanded, its log as the contract (rows, messages, every metric,
  artifacts to open or download). Table previews show the first 100 rows (`.tsv.gz` streamed and gunzipped).
- A step always runs in the image its `step.yaml` names. Pipelines choose steps, `on_fail` and parameters only.
- Typed parameters (`pipeline/runner/params.py`, including `range`) drive the forms and the API's validation.
- `step.yaml`: `description` is one sentence; `narrative` holds the longer explanation shown on the pipeline page.
- **Bulk upload** (`client/gwashub_upload.py`, plan and decisions in `docs/BULK_UPLOAD_PLAN.md`): a standard-library
  Python script downloaded from the Account page; an Excel manifest (README sheet, Manifest sheet with drop-downs; read
  on the server, `manifest_xlsx.py`) names every column exactly (nothing guessed) and repeats
  a cohort's details on its rows; `check` reports every problem by row (server-side, `routers/bulk.py`, the wizard's own
  rules) and `upload` sends nothing unless all pass, then resumes after interruptions. Duplicates are refused; a manifest
  never changes an existing cohort. Authenticated by **personal API tokens** (one project, upload only, 30 days, stored
  hashed, revocable; accepted only by the upload endpoints, `auth.upload_caller`).
- **Guided tours** (Help menu, top right; driver.js, MIT): six tours (upload a dataset, check QC and start it, review a
  dataset, run a meta-analysis, bulk upload, set up a project) defined as data in `web/app/utils/tours.ts`, pointing at
  elements marked `data-tour="…"`; they move between pages and wait when the next element only appears after the user
  acts. `api/tests/test_tours.py` fails if a tour points at an element or page that no longer exists.
- **Practice project** (`practice.py`, files in `practice/`): created at start-up (`GWASHUB_PRACTICE=0` turns it off),
  every user a reviewer so every tour works there, three synthetic files to download with the details to enter. Each
  person sees only themselves in its members, which can't be changed; its data is shared and may be deleted.
- Every upload (website or script) lands as **pending**: QC starts when the uploader (their own datasets) or a reviewer
  clicks Start QC, in the dataset panel or for a selection in the datasets table. Selected datasets can be deleted in bulk.
- Dataset status: QC pass → passed, warn → for review, fail → failed; a reviewer's decision overrides: approve →
  passed, reject → failed, request changes → **changes requested** (kept out of meta-analysis like failed). The uploader
  then sees a badge on the Datasets tab and can re-run QC or upload a corrected file with the same column names; the
  corrected file replaces the old one (deleted; the website warns this can't be undone), clears the review and starts
  QC. On AWS the uploads bucket is versioned, so a replaced file stays recoverable by an administrator until the
  lifecycle rule expires it.
- Reproducibility: pipeline versions pin resource versions; runs record the pipeline version and image digests.

**Pipelines**
- A project has several named pipelines, each versioned, one the default. A dataset follows the default unless a
  reviewer pins it to another. Changing a dataset's pipeline, or saving a new version, only advises a re-run: the
  dataset shows as out of date (latest run on another version), with counts and "Re-run all out of date".
- Changing a dataset's genome build re-runs QC automatically.

**People and permissions**
- Enforced server-side (`auth.py`). Uploaders see all rows and counts but open only their own datasets (uploaded by them, or whose contact email is theirs: `auth.owns_dataset`); reviewers do
  everything else in their project, including members, pipelines and metadata settings. Non-members get 404.
- Platform admins create projects (becoming the first reviewer), upload resources and delete projects. For anything
  else they need a reviewer role in the project.
- Meta-analysis: every member sees results and plots; only reviewers run them or download full summary statistics.
- Sign-in: local is pick-a-user; AWS is Cognito, invite-only, MFA for everyone. Sessions are HMAC-signed cookies
  (12 hours).

**Metadata**
- Fields are defined once (`metadata_schema.py`, HERMES and SGC), each per cohort or per dataset; projects choose
  which are shown and required, and can add their own (text, number, whole number, yes/no, choice, date). Project
  fields can be renamed and hidden, never deleted, so saved values and edit history stay valid.
- Datasets inherit their cohort's values and may override them; every edit is logged.
- Phenotypes are case/control or quantitative; quantitative datasets give a total N. A meta-analysis can't mix them.

**Deleting**
- Datasets: uploader or reviewer; removes the upload and all QC outputs; past meta-analyses keep their results.
- Projects: platform admins; the name must be typed (pasting blocked) and confirmed again; refused while jobs run.
  Removes every record and the project's files in all storage areas. On AWS the buckets are versioned, so files stay
  recoverable for 30 days (dev) or 90 days (prod).

**Storage paths** `uploads/<project>/<dataset>-<UTC time>/<file>`, `qc/<project>/<dataset>/<run>-<UTC time>/`,
`meta/<project>/<id>-<UTC time>/`, `resources/<shared|project>/<key>/…`: findable, and unique across database resets.

## Known behaviour

- Where an indel's shorter allele is the first base of the longer one, gwas2vcf keeps the upload's orientation, so
  EA = ALT may not hold for those indels (BETA still matches the EA).
- Synthetic datasets don't fit the real references (only ~50% of SNPs match the real GRCh37 base), so on AWS dev
  `harmonise_liftover` warns for them. Smoke-test dev with a real file.
- Job sizes come from each step's `compute:`; they don't yet scale with the uploaded file's size.
- `stats_summary` finds per-dataset lead loci by distance (a QC indicator); meta-analysis lead loci use LD clumping.
- harmonise_liftover writes a BETA of −0.0 (a zero negated by an allele switch) as 0, and the meta-analysis loader does
  the same for QC'd files written before: METAL's sample-size scheme read −0.0 as a negative effect.
- Deliberate differences from the HERMES 3 pipeline: a warning at 5% removed, missing INFO filled with 1 by default,
  chromosome 25 / XY read as X, and the gotcha checks above (none of which HERMES 3 had; only the effect-type and
  truncated-gzip checks fail).
- METAL's overlap correction bins variants by total weight; with per-variant N most bins hold one variant and the
  correction is erratic (validation test 3's dry run).
- The synthetic data's SEs (`devtools/synth.py`, "rough log-OR SE") are half what the counts imply, so
  `stats_summary`'s observed / expected SE is about 0.5 on them: inside the 0.4 to 2.5 band, but a real file should sit
  near 1.
- On MySQL (the shared host) a JSON column gives its object's keys back in MySQL's own order, not the order they were
  written in, so the dataset panel's column mapping and step metrics may be listed in a different order there than on
  Postgres. Nothing depends on the order.

## Not done


- **Revalidate GWAMA.** It now runs one chromosome at a time (memory), with genomic control per dataset applied by the
  wrapper; checked only on synthetic data so far. Validation tests 1 to 3 must be run again, plus a new one and a
  full-size run: `docs/VALIDATION_PLAN.md`, "Revalidation needed".

- **Shared host (Broad):** the Broad's values in `SHARED_HOSTS["broad"]` (`infra/gwashub_infra/config.py`, empty until they are collected: the list is in `docs/SHARED_HOST.md`), then the first deployment and smoke test: the shared IAM role's size and who manages it, MySQL and nginx set up, a browser upload through a presigned URL (the host-only condition on the S3 grants may need relaxing), a QC run on Batch, the reference files.

- Production: the plan, in order, with costs, is `docs/PRODUCTION_ROADMAP.md` (WAF, domains, isolated
  consortium steps, malware scanning, alarms, CI/CD, SES for invitations).
- Resource import from a URL (upload only).
- The posters in `docs/images/` predate the vcf and ldsc images, On-Demand meta-analyses and per-chromosome GWAMA;
  regenerate both from `docs/IMAGE_PROMPTS.md` (kept up to date) when convenient.
- `.github/CODEOWNERS` has placeholder team names; the `qc-pipeline` repository on GitHub isn't archived.
