"""Remote approval: a finished game's clips, offered on the owner's phone,
approved there with a PIN, and published to Instagram and Facebook from this
app. Basketball games only, unless the owner opts in to other videos.

Ported from this week's event branch (remote_approval/, 29 Oct to 2 Nov 2026)
and adapted to this app's store. The pieces, in the order a clip moves
through them:

* notifier.py   - the worker's "captions" stage calls in here when a job
                  finishes. It snapshots the job's clips, counts the
                  highlights, has Muse Spark write the captions, and queues
                  the offer. No relay call on the pipeline thread.
* service.py    - runs on the relay loop. Sends what the outbox holds to the
                  relay, asks the relay for decisions made on the phone, and
                  reports results back.
* previews.py   - a small video of each offered clip, made and sent one a
                  pass, so the approval page can play it.
* commands.py   - checks every decision against the offer it names before
                  anything is queued (job, clips, fingerprint, expiry,
                  platforms), and queues the publishes it allows.
* publishing.py - one approved clip, uploaded to Instagram and Facebook
                  through publish/meta_graph.py, recorded in clip_publishes.
* states.py     - the states an offer moves through and the moves allowed.
* relay.py      - the signed HTTP client for the relay (meta-relay/).
"""
