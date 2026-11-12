# Asynchronous plugin swap actions

Article Layer: 1
Article Role: plugin_reference

The plugin-action faces `plugin::macos_self_deployment_plugin::swap_status`
and `plugin::macos_self_deployment_plugin::swap_rollback` return
`{job_id, status: queued}`. One plugin-owned worker calls the original business
method and stores its complete response envelope. Poll
`service_interface::job_service::get_job` with the exact job id until terminal;
read `job.result.data` for the original router snapshot or rollback outcome,
and preserve `job.error` on failure. A queued receipt does not prove that
routing changed. Rollback eligibility is evaluated when the worker runs; the
existing drain window still applies.

The public `service_interface::local_self_deployment_service::swap_status` and
`swap_rollback` faces remain synchronous and retain their original schemas,
results and errors on the existing service engine executor. The underlying
Python methods remain synchronous for service and worker callers. This action
change does not alter those public contracts.

The existing job ledger supplies identity, history, readback, terminal status
and cancellation. A local job already marked cancelled when its worker admits
it is skipped. Cancellation racing with admission does not reverse effects;
rollback is an explicit operation.
The single daemon retires when its local queue drains. A later submission
starts a new sole worker; another runtime instance never replays these local
handoffs. A crash or failed outcome write requires reconciliation of the named
job and actual router state before retrying an effect.
