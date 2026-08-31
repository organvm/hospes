# Studio Routing

## The Three-City Model

The studios are not peripheral infrastructure borrowed from Ari's other operations. They are
**owned operating infrastructure**, and the system is designed around them from the beginning.
The podcast can be presented as a new format from an established production operation, even though
the show itself has no prior audience.

The guest proposition becomes:

> We record professionally in Los Angeles, New York, and Austin. We can route the appearance
> around your existing travel schedule, handle the entire production process, and require no
> promotional commitment.

That is a legitimate offer independent of follower counts or friendship favors.

## Node Profiles

### Los Angeles

Primary sourcing profile: comedians, actors, writers, directors, musicians, creators, entertainment
executives, podcasters, producers, publicists, managers. The AI monitors entertainment release
cycles and touring schedules for routing intelligence.

### New York City

Primary sourcing profile: writers, journalists, academics, artists, designers, publishers,
comedians, media figures, founders, cultural institutions. NYC is where the show's broader
intellectual and artistic editorial identity can be established alongside its comedy credibility.
Guests from literature, art, academia, technology, and cultural theory — not only comedy — belong
here.

### Austin

Primary sourcing profile: touring comedians, musicians, founders, technologists, independent
creators, festival guests, cultural and political commentators, people passing through for major
events. Austin functions as a bridge between comedy, technology, music, and entrepreneurship.

These are sourcing hypotheses, not rigid categories. The guest's actual travel schedule and the
available recording window determine the final routing.

## Studios as First-Class Objects

Every guest opportunity automatically includes a studio record:

```
preferred_city:
alternative_cities:
guest_travel_schedule:
studio_availability:
host_availability:
room_configuration:
crew_requirements:
recording_duration:
setup_and_reset_buffers:
hospitality_requirements:
technical_requirements:
best_proposed_dates:
```

The workflow becomes:

```
Candidate identified
→ editorial thesis created
→ public travel schedule checked
→ nearest studio selected
→ studio inventory checked
→ outreach sent with concrete city and window
→ guest confirms
→ room, crew, equipment, intake, release, and hospitality reserved
→ episode recorded
→ post-production and relationship follow-up triggered
```

## AI Roles

### City Intelligence Agent

Maintains a live calendar for each city:

```
Guest
Reason they are in the city
Arrival and departure window
Public event
Representation
Editorial relevance
Likely availability
Nearest studio
Recommended outreach date
```

Detects opportunities such as: "This person will be in Los Angeles for three days promoting a film
and has an unfilled afternoon between appearances."

### Guest-to-Studio Router

Matches each guest to the best recording location using: guest home city, travel schedule, studio
availability, host location, production staff availability, editorial priority, travel cost,
required equipment, and any security or privacy requirements.

Example output:

```
Guest: [Name]
Home market: New York
Current travel: Los Angeles, September 8–11
Preferred recording: In person

LA studio: Available September 10 at 2:00 PM — host available — no travel required
NYC studio: Available September 19 — guest would need separate scheduling

Recommendation: LA September 10
```

### Capacity Optimizer

Prevents studios from being treated as disconnected calendars. Manages recording blocks, setup and
teardown time, crew assignments, equipment needs, guest buffers, delays, maintenance windows,
multi-show scheduling, and empty studio inventory. Batches recordings intelligently:

```
Tuesday — NYC
  11:00 AM: Author
   2:00 PM: Comedian
   5:00 PM: Founder
```

Rather than scattering identical logistics across three separate days.

### Touring-Guest Interceptor

Tracks guests already traveling between markets. Example:

```
Guest is performing:
  Austin — August 4
  Los Angeles — August 9
  New York — August 15

Recommendation: Approach through manager for Los Angeles.
Studio availability matches afternoon before performance.
```

The invitation becomes: "We know you are already in town. We have a studio, crew, and a specific
episode ready." Not: "Can you do Ari a favor?"

### Cross-City Executive Producer

Manages the national slate so the same guest is not accidentally contacted from different cities.
Decides which studio owns the opportunity, which producer owns communication, which host is
present, whether a guest should be held for a later city, whether several guests can be assembled
around one production week, and whether a thematic series should be recorded entirely in one
location.

## Batching Doctrine

Record multiple guests per studio day whenever the editorial slate supports it. The Capacity
Optimizer proposes batched windows; the Cross-City EP approves and coordinates. Batching improves
crew efficiency and reduces logistical overhead per episode without reducing production quality.

## Studio-Generated Network Surfaces

Each location should support occasional curated events:

- Small roundtables
- Live podcast tapings
- Private studio conversations
- Writer or artist gatherings
- Comedy and technology salons
- Listening sessions
- Cross-podcast gatherings

The purpose is not generic networking. It is to create structured environments where future guests
naturally encounter the show. Example: six people invited to a private studio conversation. Two
later become episode guests. One introduces a third. One becomes a recurring collaborator. That
is network creation rather than network extraction.

The studios also enable reciprocal value before a booking is requested: letting a colleague use a
room, connecting guests with other producers, offering clean clips and photographs, recording
someone during an otherwise inconvenient tour stop.

## The Routing Workflow (Summary Line)

> Identify best city → check travel schedule and studio inventory → outreach with concrete window
> → confirm → reserve room, crew, equipment, and hospitality → record → follow up.

## First-Season City Distribution

| Location | Approximate episodes | Strategic function |
|---|---:|---|
| Los Angeles | 4–5 | Establish comedy and entertainment credibility |
| New York | 4–5 | Establish the show's intellectual, artistic, and cultural identity |
| Austin | 2–4 | Establish comedy, technology, music, and founder crossover |

The split is not arithmetically rigid. The goal is demonstrating genuine geographic and editorial
range. A stronger pilot sequence prioritizes the episode fit over the city count.

## Executable Seed

`hospes/routing.py` implements the Guest-to-Studio Router: given a guest record and studio
availability calendar, it returns a ranked list of city/date options with travel cost and social
cost scores. See that file for the data contract and extension points.
