# Productization

How HOSPES is positioned, sold, and expanded from one flagship show to a network product.

---

## What we are selling

**An invisible guest, production, and distribution desk.**

Not "AI podcast automation." Not a tool. Not a chatbot. A desk — the institutional production office a major podcast would normally staff with a talent coordinator, a researcher, a scheduler, a producer, and a relationship manager — installed around a show with almost none of that staff.

The intelligence is backstage. The host, the guest, the relationship, and the performance remain human. The guest never knows a system was involved. Host never manages a spreadsheet.

The framing parallel is Aerarium: "institutional weight for one person, zero staff." HOSPES is the media equivalent of that proposition.

---

## The first product requirement: host-effort budget

The primary product requirement is not a feature. It is a constraint:

> **Host's operational load must not exceed approximately 10 minutes per week.**

Everything else — feature scope, agent design, approval flow, interface design — is subordinate to this constraint. If a workflow requires more than 10 minutes of host attention per week, it is a product defect, not a UX problem.

This constraint implies:

- One digest surface, not an inbox.
- Every card in the digest supports exactly four actions: APPROVE, REJECT, PROTECT RELATIONSHIP, ADD PERSONAL NOTE.
- Anything that cannot be decided with those four actions does not belong in the host's view. It belongs in the producer's queue.
- The system must do all research, routing, and drafting before the host sees a candidate. No half-formed cards.
- Calendar negotiation, follow-up tracking, and logistics never surface to the host.

The 10-minute budget is the north star for every product decision.

---

## What the product is not

Stating these explicitly prevents the wrong product from being built:

- It is not a contact database for sale. Guest contact information is tenant-local, never pooled, never sold.
- It is not a celebrity promise. The system cannot deliver any specific guest. It makes the best-possible outreach from the best-possible route at the right moment.
- It is not a producer replacement. The producer seat is a required human role. The system gives one producer the leverage of three; it does not eliminate the need for a producer.
- It is not a marketing automation tool. Mass outreach, cold sequences, and drip campaigns are architectural exclusions.
- It is not visible to guests. No guest portal branding, no system name in correspondence. The guest team is a real person (the producer) backed by invisible infrastructure.

---

## The managed-service-first ladder

The productization sequence is deliberate. Do not attempt to build a self-serve SaaS before the system has been proven as a managed operation.

### Step 0: Operate the flagship

Run HOSPES for the Host and Producer show. Own the full stack: dossiers, outreach, scheduling, episode production, relationship stewardship, distribution. Build nothing for outside customers yet. The only goal is to make the flagship run on the 10-minute host budget consistently.

This step proves:
- The workflow actually works in production conditions.
- The approval flow matches how Host actually makes decisions.
- The agent outputs are good enough to send under a real producer's name.
- The editorial thesis and segment design survive real guest conversations.

### Step 1: Install for one or two network shows

Once the flagship has run for several episodes without system-caused problems, install HOSPES for one or two other shows inside the same network. These are shows Producer already has relationships with, not cold outside customers.

The install is still managed: the system operators (Host and Producer) configure and run it. The outside show gets the benefit; the operators retain control and learn what is genuinely configurable versus what is flagship-specific.

This step proves:
- Show DNA configuration works as a real isolation boundary.
- A new show can be onboarded without breaking the flagship tenant.
- The relationship graph stays correctly permissioned across shows (a network-level relationship is not automatically available to every show).

### Step 2: Productize the repeated layer

After two or three successful outside installs, the parts that were repeated across every install become the product layer: the control plane, the Show DNA schema, the onboarding flow, the permission model, the approval workflow.

Only at this point does self-serve or lighter-touch installation make sense, and only for the layer that has actually been proven repeatable.

---

## The cross-show permissioned guest-routing moat

The most defensible feature of HOSPES at network scale is not any individual agent. It is the permissioned relationship graph.

When a network has multiple shows, a guest who appeared on Show A is a potential guest for Show B — but only under explicit conditions:
- The relationship owner at Show A must grant cross-show routing permission.
- Protected relationships are never surfaced across show boundaries without explicit owner consent.
- Guest contact data remains scoped to the show that acquired it unless explicitly shared.
- The system tracks which route was used for which show, so the same contact is never approached twice from different shows without coordination.

This creates a compounding moat: every episode adds relationship capital that makes future booking cheaper across the whole network. No single-show competitor can replicate it without the network effect.

---

## Multi-tenant proof order

Tenancy is not a later feature. It is built from day one, proven in this order:

### Tenant 1: Flagship show (Host + Producer)
- Two hosts, one producer, LA/NYC/Austin studios.
- Full feature set: candidate pipeline, protected relationships, outreach, scheduling, episode production, clip yield, distribution.
- Proves the system works as an operator-facing product, not just a personal tool.

### Tenant 2: Field show (Producer)
- One host, portable kit, guest environments.
- Same guest desk, relationship graph, archive, and distribution system.
- Different Show DNA: Place → Object → Intervention → Artifact format engine.
- Proves that Show DNA configuration creates real isolation, not just cosmetic theming.

### Tenant 3: One outside podcaster from the network
- Real customer with their own show, guests, studios, and host relationships.
- Managed install: operators configure and support.
- Proves that the product runs for someone who did not build it.
- This tenant is the minimum threshold before any "productized" or self-serve story is told to the market.

---

## Aerarium framing parallel

Aerarium's proposition: institutional weight for one person — the governance, treasury, and process infrastructure of an organization, operated by a principal with no staff.

HOSPES is the media equivalent:

> The institutional production office a major podcast host would normally hire — talent coordinator, researcher, scheduler, producer, relationship manager — installed around a podcaster whose operational budget is approximately 10 minutes per week.

The intelligence is invisible. The relationship remains human. The output looks like it came from a well-resourced production team.

This framing is how the product is explained to a potential customer, to a network operator, and to a potential press or investor context. It is not a technical description. It is what the customer actually buys.
