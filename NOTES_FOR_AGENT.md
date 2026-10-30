# Deployment safety for agents

- Availability is non-negotiable. Never start a deployment without a tested, immediate recovery plan. A failed update must automatically attempt to restore the previous healthy deployment and reopen traffic once it is ready.
- Never knowingly leave maintenance enabled indefinitely. If recovery fails, act immediately to restore service, report the situation, and continue recovery; do not abandon the deployment.
- Prepare downloads and compatibility checks before downtime. Preserve the single-writer boundary and the stopped-volume backup. Never promise zero downtime or guaranteed recovery: host, storage, process termination, and other failures can prevent either.
- Do not attempt automatic old-image recovery after an incompatible schema or data migration. Such upgrades need a separately tested stopped-writer migration and backup restoration plan before maintenance begins.
