# WSL `orion-neo4j` crash loop (fixed 2026-10-31)

Found and fixed while building the graph-as-memory Q&A pilot (`reports/Graph-as-memory Q&A pilot.md`) on the Windows dev
box, whose Neo4j runs in WSL `Ubuntu`.

- **Symptom.** After the WSL distro started (about 19:02 UTC), `orion-neo4j` restarted in a loop: exit code 3,
  "Neo4j Server shutdown initiated by request" right after "Logging config in use", more than 10 restarts.
  Nothing in `debug.log`.
- **Diagnosis.** A one-off `docker run` of the same image on the same `orion_orion_neo4j_data` volume started
  cleanly. So the data was fine; the old container's own filesystem was stale.
- **Fix (19:09 UTC).** Recreate the container from Orion's compose file, keeping the named data volume:

  ```bash
  cd ~/orion && docker compose up -d --force-recreate orion-neo4j
  ```

  This restores the `unless-stopped` restart policy. Only the anonymous `/logs` volume is new; the old logs stay
  in the earlier anonymous volume (`fcb08589...`).
- **Side effect, undone.** A probe briefly created an empty volume named `orion_neo4j_data` (one underscore
  prefix short of the real one). It was removed; the real volume is `orion_orion_neo4j_data`.
- **If it loops again** after a distro restart, the same recreate should fix it.
- **Related.** WSL idle-stops the distro, which stops Neo4j mid-work (CLAUDE.md, Environment). For long runs from
  Windows keep a `wsl` shell open, e.g. `wsl -d Ubuntu -- bash -lc "while true; do sleep 50; done"`.
