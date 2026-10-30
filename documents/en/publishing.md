# Collect and publish with one Douyin login

The console's **Publish video** page uses the same encrypted QR account source as collection. A saved QR login appears in the account selector. An identity created only by pasting a Cookie header needs a QR login before it can supply a complete browser session.

Administrators can select an account, upload an MP4/MOV/M4V file of up to 1 GiB, and enter a title (30 characters) and description (1,000 characters). **Upload locally and check account** stores the file on this instance and verifies the creator's name and UID without publishing. **Confirm and publish to Douyin** shows that account and content for explicit confirmation, then uploads and submits once.

The result is verified against a new matching work in the creator work list. Submission is separate from platform approval. This integration does not set content-origin declarations, enable third-party synchronization, schedule publication, or rotate publishing accounts. Advanced publication settings must be handled in the platform's creator center.

## Session and failure handling

Complete browser state is encrypted in the existing Vault. Older Cookie-only records still load. A login change invalidates an earlier confirmation; a stale browser cannot overwrite a newer scan. After checking or publishing, the console synchronizes the refreshed login into the collection identity. A failed sync can be retried independently.

The final-click boundary is stored before clicking. A timeout or process restart after that point produces an **uncertain** result, never an automatic retry. Check the platform's work management before creating another publishing job. Additional platform verification stops the job.

Upload storage is limited to 10 GiB. Completed/uncertain submissions remove their local media. Other media older than a day is removed at the next cleanup; drafts can also be discarded explicitly. Local deletion never deletes a platform work. Publishing history is persisted in SQLite under `qr-data/publishing/`. Run only one account-service process per data directory.

## Agent API

All routes use `/api/v1/admin/publishing` and require an administrator session or an administrator-owned API key with `admin` scope. Credentials are never returned to the client.

1. `GET /` (use the prefix without a trailing slash): list account summaries and the latest 100 jobs.
2. `POST /jobs`: send `account_id`, a unique 32-character hexadecimal `client_key`, `title`, `description`, a video `filename` without a path, and exact `size` in bytes. Reuse the key and payload when retrying draft creation.
3. `PUT /jobs/{id}/video`: stream the raw video bytes.
4. `POST /jobs/{id}/prepare`, then poll `GET /jobs/{id}` until `ready` and verify the creator identity.
5. `POST /jobs/{id}/sync`: synchronize collection login after `ready` or `published`.
6. `POST /jobs/{id}/publish` with `{"account_id":"verified account ID","confirm":true}` only after authorized publication is confirmed. Poll the same job after a lost response; do not create another job to repeat it.
7. `DELETE /jobs/{id}`: discard a local draft when it is not running.

## Docker

Use the existing `compose.qr.yaml`, `.env.qr` and persistent volumes. Back up the database, configuration and entire `qr-data` volume, including its original key, records and pending media. The application image is `dtk-qr-app:5.1.7-unified1`; the account service is `dtk-qr-login:0.3.0`. PostgreSQL remains at migration `0018`.

Validation covers fresh Docker installation, actual creator login with an existing account, and submission through an intercepted creator site in a real headless browser. Development verification did not publish a real work. Linux/amd64 is the tested container architecture. XiaoHongShu, image posts, scheduling and batch publishing are outside this version's publishing scope.
