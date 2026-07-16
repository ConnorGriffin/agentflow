# Open-source license and contribution rights

Research for [issue #138](https://github.com/ConnorGriffin/agentflow/issues/138),
completed 2026-07-16.

## Decision

**Recommendation:** launch the public agentflow core under **Apache License 2.0**,
accept contributions under **inbound-equals-outbound terms plus Developer Certificate
of Origin 1.1 sign-off**, and require **no contributor license agreement or copyright
assignment**.

Contributors should retain their copyright. `CONTRIBUTING.md` should state that an
intentional contribution is licensed under Apache-2.0, require a `Signed-off-by` line
certifying the [DCO 1.1](https://developercertificate.org/), and require disclosure of
third-party material and authority to contribute employer-owned work. Enforce the DCO
on every outside-contributor commit; handle project-controlled bots under a separate,
counsel-approved provenance policy. Keep the project name, logo, and “official” service
marks under a separate trademark policy.

**Inference:** this is the best match for the chosen NetBox-style model. It gives
companies a familiar permissive license with an express patent grant; gives the
community the same rights Connor receives; and leaves Connor free to sell hosting,
support, SLAs, managed upgrades, integrations, enterprise packaging, and
Apache-compliant proprietary derivatives. Under Apache-2.0, each Contributor grants
every licensee broad copyright permissions and a license to relevant patent claims; the
license permits sublicensing and derivative works, permits different terms for
modifications or derivative works when the Apache conditions remain satisfied, reserves
trademarks, and permits charging for support or warranty obligations.
([Apache-2.0 sections 2–6 and 9](https://www.apache.org/licenses/LICENSE-2.0))

The trade is real: Apache-2.0 also lets competitors self-host, fork, close their own
modifications, and offer competing services. A genuine open-source license cannot
reserve a field of endeavor or prohibit competitive hosting; commercial use is part of
the [Open Source Definition](https://opensource.org/osd) and is confirmed by the
[OSI's commercial-use FAQ](https://opensource.org/faq). The moat must therefore be the
official brand, release stewardship, product quality, operating expertise, trust, and
service—not an exclusive right to run the code.

**Lawyer review is required before launch and before accepting the first outside
contribution.** This note is decision support, not legal advice.

## Repository baseline

**Repository observations:** direct inspection of the worktree and complete git history
on 2026-07-16 found:

- No tracked `LICENSE`, `COPYING`, `NOTICE`, `CONTRIBUTING`, `DCO`, `CLA`,
  `CODE_OF_CONDUCT`, or `GOVERNANCE` file. Without a license, default copyright applies;
  GitHub users receive platform rights to view and fork, not general rights to use,
  modify, or distribute the project. [GitHub documents that distinction directly](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/licensing-a-repository).
- [`pyproject.toml`](../../pyproject.toml) has project name, version, description,
  dependencies, and build metadata but no license, authors, or license-file metadata.
  [`agentflow/webui/package.json`](../../agentflow/webui/package.json) is private and
  also has no license field.
- All 209 commits have the same email and one of two name spellings: 144 as
  `ConnorGriffin` and 65 as `Connor Griffin`. No commit has a human third-party author.
  Commit messages contain 111 `Co-authored-by` trailers naming Anthropic model accounts
  (84 Opus 4.8, 18 Sonnet 4.6, and 9 Fable 5), and no `Signed-off-by` trailer.

The authorship and file observations are reproducible with:

```sh
git ls-files | rg '(^|/)(LICENSE|COPYING|NOTICE|CONTRIBUTING|DCO|CLA|CODE_OF_CONDUCT|GOVERNANCE)(\.|$)'
git rev-list --count --all
git log --all --format='%aN <%aE>' | sort | uniq -c
git log --all --format='%B' | rg -i '^(Co-authored-by|Signed-off-by):'
```

**Inference:** this is an unusually clean time to choose the outbound license because
no human third-party author appears in git's author fields. It is not proof of chain of
title: copied snippets, vendored assets, employment obligations, model-generated code,
or an author/committer mismatch can create rights questions that a shortlog cannot
answer.

The AI trailers require a specific legal check, not panic. Anthropic's current
commercial terms say that, as between the parties and to the extent allowed by law, the
customer owns outputs and Anthropic assigns any output rights it has; OpenAI states a
similar assignment for output. ([Anthropic commercial terms §B](https://www.anthropic.com/legal/commercial-terms),
[OpenAI Terms, “Ownership of content”](https://openai.com/policies/terms-of-use/))
Those contracts do not settle copyrightability or third-party provenance. The U.S.
Copyright Office maintains that human authorship is required while recognizing
copyright in human-authored selection, arrangement, or modification of AI material.
([Copyright and Artificial Intelligence, Part 2](https://www.copyright.gov/ai/Copyright-and-Artificial-Intelligence-Part-2-Copyrightability-Report.pdf))

## License-family comparison

Labels in the table distinguish the license text or steward's documented explanation
from the decision inference for agentflow.

| Family | Documented fact | Inference for agentflow | Result |
| --- | --- | --- | --- |
| **Permissive: Apache-2.0** | Grants broad copyright rights, sublicensing, an express contributor patent license with defensive termination, inbound contribution terms, notice obligations, and no trademark license. It allows added terms for modifications or derivative works if the original Apache conditions remain satisfied. [Official text](https://www.apache.org/licenses/LICENSE-2.0) | Low-friction company adoption plus stronger patent and contribution clarity than MIT. It preserves hosted and enterprise options without giving Connor rights the community lacks. | **Choose.** |
| **Permissive: MIT** | Allows use, modification, sublicensing, sale, and distribution if the copyright and permission notice is retained. The text contains no express patent grant or contribution clause. [OSI license text](https://opensource.org/license/mit) | A viable runner-up, but Apache's patent grant, inbound clause, change notices, and explicit trademark boundary are worth the extra text for company-facing automation software. | Reject in favor of Apache-2.0. |
| **Weak copyleft: MPL-2.0** | MPL's copyleft applies at the file level: modified covered files remain MPL and their source must be offered on distribution, while separate files in a larger work can remain proprietary. It includes contributor patent rights. [Mozilla MPL FAQ](https://www.mozilla.org/en-US/MPL/2.0/FAQ/) and [license text](https://www.mozilla.org/en-US/MPL/2.0/) | A credible second choice if reciprocal distribution of core fixes becomes more important than maximal adoption. It does not stop a competitor from running private modifications as a service because its obligations arise on distribution, and it introduces file-boundary compliance work. | Do not choose for the stated priority order. |
| **Weak copyleft: LGPLv3** | LGPLv3 is structured around a library, applications that use its interface, and linked combined works; it adds relinking and modification protections to GPLv3. [Official text](https://www.gnu.org/licenses/lgpl-3.0.en.html) | The licensing unit does not match agentflow, which is an application and service rather than a reusable library. | Reject; reconsider only for a separately shipped library. |
| **Strong copyleft: GPLv3** | A conveyed modified work must be licensed as a whole under GPLv3, and object-code conveyance requires corresponding source. Network interaction alone is not conveyance. [GPLv3 sections 5–6](https://www.gnu.org/licenses/gpl-3.0.en.html) and [AGPL's explanation of the GPL network gap](https://www.gnu.org/licenses/agpl-3.0.en.html) | More integration and distribution review for adopters than MPL, without requiring a private hosted fork to publish its changes. | Reject. |
| **Network copyleft: AGPLv3** | A modified network-accessible version must prominently offer its remote users corresponding source. [AGPLv3 §13](https://www.gnu.org/licenses/agpl-3.0.en.html) | Best protection against closed hosted modifications, but the poorest fit for the stated broad-adoption priority. It also creates a need for a broad CLA or assignment if Connor wants to sell a non-AGPL license to the exact contributor-built core. | Reject unless anti-SaaS reciprocity becomes the primary goal. |

**Documented corporate-policy evidence:** this is not merely a theoretical compliance
ordering. Google's first-party policy treats permissive licenses such as Apache and MIT
as notice licenses, treats MPL as reciprocal, restricts GPL/LGPL use, and prohibits AGPL
use except for narrow exceptions. The Apache Software Foundation likewise accepts
Apache/MIT-style dependencies as Category A, handles MPL as conditional Category B, and
excludes GPL/LGPL/AGPL dependencies from ASF products. These are two prominent policies,
not a market-wide adoption study. ([Google third-party license policy](https://opensource.google/documentation/reference/thirdparty/licenses),
[ASF resolved license policy](https://www.apache.org/legal/resolved.html))

**Recommendation:** do not use a Business Source License, “community” license, or a
custom no-competing-hosting term. HashiCorp describes BSL as source-available and says
its competitive-offering restriction gives the sponsor more commercialization control;
that restriction is exactly why it is not an OSI open-source model. ([HashiCorp's license-change announcement](https://www.hashicorp.com/en/blog/hashicorp-adopts-business-source-license),
[Open Source Definition §6](https://opensource.org/osd))

## Inbound-rights comparison

| Mechanism | Documented fact | Inference for agentflow | Result |
| --- | --- | --- | --- |
| **Repository contributor terms / inbound equals outbound** | Apache-2.0 §5 says intentional submissions are under Apache-2.0 unless explicitly stated otherwise. GitHub's Terms say content added to a licensed repository is licensed on the same terms and the contributor agrees it has the right to do so. ([Apache §5](https://www.apache.org/licenses/LICENSE-2.0), [GitHub Terms §D.6](https://docs.github.com/en/site-policy/github-terms/github-terms-of-service)) | This supplies the actual inbound copyright and patent license with almost no process. A clear `CONTRIBUTING.md` makes the rule visible, including outside GitHub, but does not produce a per-commit provenance attestation. | Use as the legal baseline. |
| **DCO 1.1** | A sign-off certifies that the contribution is the signer's work or appropriately licensed work and that the signer has the right to submit it under the indicated open-source license. It preserves a public contribution record. [Official DCO text](https://developercertificate.org/) | Adds useful origin and authority evidence without extra relicensing rights, ownership transfer, or a separate negotiated contract. The small per-commit burden is proportionate for human and corporate contributors; project-controlled automation needs a distinct provenance rule. | **Require for every outside contribution.** |
| **Contributor License Agreement** | A CLA can leave ownership with the contributor while granting a project explicit copyright, sublicensing, patent, employer-authorization, and representation rights. ASF requires an ICLA for maintainers and large contributions but accepts small contributions under Apache §5; Grafana uses an Apache-style CLA for every contributor. ([ASF contributor agreements](https://www.apache.org/licenses/contributor-agreements.html), [ASF ICLA](https://www.apache.org/licenses/icla.pdf), [Grafana CLA](https://grafana.com/docs/grafana/latest/developer-resources/contribute/cla/)) | A CLA is justified when the project needs rights beyond the outbound license—for example, proprietary relicensing of AGPL community code. Under Apache-2.0, it adds administration and founder/community asymmetry without being necessary for hosted services or Apache-compliant enterprise code. A document called “Contributor Terms” is still a CLA if its substance grants these extra rights. | No CLA at launch. Add only after counsel identifies a concrete missing right. |
| **Copyright assignment** | Assignment transfers copyright ownership. The FSF uses assignment to centralize copyleft enforcement, add permissions, and update licenses or exceptions, while granting contributors rights back. [FSF contributor FAQ](https://www.fsf.org/licensing/contributor-faq) | Maximum relicensing and enforcement control, but maximum contributor friction and trust cost. Its strongest rationale is centralized freedom enforcement, not a permissive commercial-community launch. | Reject. |

**Inference:** Apache-2.0 + DCO intentionally makes a future unilateral license switch
harder than a broad CLA or assignment. Connor can still use all Apache-licensed
contributions in hosted services and compliant proprietary derivatives, but cannot
erase the Apache rights from community-owned code or reissue that exact code under an
incompatible exclusive license without the necessary copyright permissions. That
constraint is a feature for a “genuinely open community product”: contributors are not
quietly financing a private relicensing option.

## Relevant project evidence

**Documented fact — NetBox:** NetBox Community is Apache-2.0, its current contribution
guide requires code submissions to be original work but states no DCO or CLA
requirement, and NetBox Labs offers both a fully hosted NetBox Cloud product and a
self-managed, supported NetBox Enterprise product. ([NetBox license](https://raw.githubusercontent.com/netbox-community/netbox/main/LICENSE.txt),
[contribution guide](https://raw.githubusercontent.com/netbox-community/netbox/main/CONTRIBUTING.md),
[NetBox Cloud](https://netboxlabs.com/netbox-cloud/),
[NetBox Enterprise](https://netboxlabs.com/netbox-enterprise/))

**Inference:** NetBox is direct evidence that a permissively licensed community core
can coexist with official hosted and enterprise offerings. It is not evidence that the
license caused adoption or guarantees agentflow's commercial success. Requiring DCO is
one deliberate step stronger than NetBox's visible repository policy: it improves the
rights record while preserving inbound/outbound symmetry.

**Documented fact — GitLab:** GitLab CE is MIT while GitLab EE uses a more restrictive
enterprise license. Its inbound policy uses DCO for MIT-licensed code and an individual
or corporate CLA for proprietary code. ([GitLab licensing](https://docs.gitlab.com/development/licensing/),
[GitLab DCO/CLA policy](https://about.gitlab.com/community/contribute/dco-cla/))

**Inference:** this is a useful precedent for adding a real proprietary boundary later:
keep community-core contributions symmetric, and use separate rights only for code
whose outbound terms are actually proprietary.

**Documented fact — Grafana:** Grafana moved its core from Apache-2.0 to AGPLv3, requires
an Apache-style CLA, offers proprietary Enterprise builds and plugins, and sells
commercial licenses to customers that want to modify and offer Grafana as a service
without AGPL obligations. ([Grafana licensing](https://grafana.com/licensing/),
[Grafana CLA](https://grafana.com/docs/grafana/latest/developer-resources/contribute/cla/))

**Inference:** Grafana proves that AGPL plus broad contributor grants can preserve a
commercial-license path. It is the right comparison if agentflow later prioritizes
anti-SaaS reciprocity over low-friction adoption; it is not the chosen NetBox-style
model.

**Documented fact — OpenTofu/HashiCorp:** OpenTofu is MPL-2.0 under Linux Foundation
governance and uses DCO; it forked the last MPL-licensed Terraform after HashiCorp moved
future Terraform releases to BSL. ([OpenTofu repository](https://github.com/opentofu/opentofu),
[OpenTofu's first-party account](https://opentofu.org/blog/the-opentofu-fork-is-now-available/),
[OpenTofu DCO record](https://opentofu.github.io/legal-documents/2024-04-03%20HashiCorp%20C%26D/SCO.html),
[HashiCorp announcement](https://www.hashicorp.com/en/blog/hashicorp-adopts-business-source-license))

**Inference:** the episode illustrates both sides of weak copyleft: MPL allowed an open
fork, while centralized rights let the original sponsor change only future releases.
For agentflow, taking extra relicensing rights now would weaken the intended community
signal without solving a stated launch need.

## Concrete launch model

**Recommendation:** before describing agentflow as open source or soliciting outside
code, make one reviewed licensing change with this shape:

1. Add the exact, unmodified Apache License 2.0 text as `LICENSE`. Record the copyright
   holder and year separately, in the locations counsel approves. Preserve all required
   third-party notices; use `NOTICE` only for attribution notices that belong there.
   [ASF application guidance](https://www.apache.org/legal/apply-license) explains the
   expected license and notice structure.
2. Add `CONTRIBUTING.md` that links the DCO 1.1, states inbound equals outbound, says
   contributors retain copyright, requires authority for employer-owned work, and
   requires third-party and AI-generated material to be disclosed when provenance or
   rights are not obvious. Counsel should draft or approve the exact legal language.
3. Require `Signed-off-by: Name <email>` on every outside-contributor commit and add a
   required DCO check. An AI agent must not synthesize a human sign-off: the Linux
   kernel's first-party AI policy places review, sign-off, and responsibility on the
   human submitter. Define how squash commits preserve that attestation and have counsel
   define the exemption and provenance rules for project-controlled bots or autonomous
   commits. Do not rewrite existing history merely to add sign-offs; have counsel confirm
   the initial-owner baseline instead. [Linux kernel AI-assistant policy](https://docs.kernel.org/process/coding-assistants.html#signed-off-by-and-developer-certificate-of-origin)
4. Add `Apache-2.0` as the Python package's SPDX license expression and include the
   license file in built artifacts. Python's current metadata specification defines the
   `License-Expression` field through [PEP 639's license-expression specification](https://packaging.python.org/en/latest/specifications/license-expression/).
   Add matching license disclosure to the frontend package and README.
5. Publish a trademark policy before offering an official service. Apache-2.0 itself
   does not grant rights to the licensor's product names or marks beyond customary
   descriptive use. [Apache-2.0 §6](https://www.apache.org/licenses/LICENSE-2.0)
6. Keep any future proprietary enterprise code in an explicit package or repository
   boundary with its own outbound license and inbound policy. Prefer monetizing service,
   operations, support, certification, integrations, and packaging before withholding
   core functionality; that better preserves the stated community-product intent.

## Why the alternatives lose

- **MIT loses to Apache-2.0** because it omits an express patent grant and inbound
  contribution clause without buying a meaningful adoption advantage established by
  this research.
- **MPL-2.0 loses** because file-level reciprocity adds compliance and architecture
  boundaries yet does not reach private hosted modifications. Choose it only if keeping
  distributed modifications open becomes a higher priority.
- **LGPLv3 loses** because agentflow is not a library licensing problem.
- **GPLv3 loses** because it adds whole-work distribution obligations without closing
  the hosted-service gap.
- **AGPLv3 loses** because it optimizes against closed hosted forks and pushes more
  adopters into legal review; preserving a proprietary license to the same community
  code would then require asymmetric inbound rights.
- **A CLA loses** because Apache-2.0 already grants the rights needed for the chosen
  business model. Collecting extra rights before a concrete need creates process and
  trust cost.
- **Copyright assignment loses** because centralized ownership is disproportionate to
  the launch need and is least consistent with a community-owned product.
- **Repository terms alone lose to repository terms plus DCO** because DCO adds a
  durable, per-commit origin and authority certification at modest cost.
- **Source-available or no-compete terms lose** because they would abandon the explicit
  requirement for a genuinely open-source product.

## Legal and operational follow-up

**Recommendation — legal:** retain counsel experienced in open-source software to review
all of the following before launch:

- The initial copyright holder: Connor personally, an existing entity, or a new entity;
  and any employer, client, or school claims.
- The complete git history and representative source/assets for copied material,
  generated code, and third-party notices. Provider output-ownership clauses are useful
  but do not replace provenance review.
- The exact `LICENSE`, copyright notice, `NOTICE`, DCO, contribution, AI-assistance, and
  trademark language.
- Whether “enterprise” means services and separate add-ons, an Apache-compliant
  proprietary derivative, or a clean non-Apache license to the exact community core.
  The last meaning is a materially different rights strategy.
- Trademark availability and registration strategy for “agentflow” and any logo.
- Export, warranty, privacy, and hosted-service terms when an actual service exists;
  those are outside this license decision.

**Recommendation — operations:** before the first public release, inventory direct,
transitive, vendored, frontend, generated, and bundled dependencies; preserve required
licenses/notices in source, wheels, containers, and committed frontend artifacts; add
license metadata and a README license section; enable the DCO check; document how bots
and autonomous agents are exempted and how accountable people review or attest their
output; and record exceptional third-party grants outside the normal DCO path. Revisit a
CLA only through a documented decision that names the specific right Apache-2.0 and DCO
fail to provide.

## Remaining uncertainty

**Remaining uncertainty:** the supplied issue question was treated as authoritative
because issue #138's body was not readable through the available GitHub connection.
No unseen issue discussion was incorporated.

**Remaining uncertainty:** no primary-source survey establishes one license as the
universal preference of company legal departments. The Google and ASF policies show
real compliance differences, while NetBox, GitLab, Grafana, and OpenTofu show viable
models; they do not prove causation or forecast agentflow adoption.

**Remaining uncertainty:** the commercial product boundary is not yet concrete. If the
real requirement becomes “force every hosted modifier to publish changes and sell them
a proprietary escape hatch,” the answer changes to AGPLv3 plus a lawyer-drafted CLA.
If the requirement becomes “keep distributed core fixes open but allow proprietary
adjacent files,” MPL-2.0 plus DCO becomes the leading option. Neither is justified by the
current NetBox-style goal.

**Remaining uncertainty:** repository authorship is concentrated, but only legal review
can confirm chain of title, especially for AI-assisted changes and any material copied
from other projects. The launch license should not be applied until that review is
complete.
