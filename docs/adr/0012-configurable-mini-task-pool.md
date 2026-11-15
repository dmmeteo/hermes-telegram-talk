# 0012 A configurable mini-task pool with independent dispatch

Status: decided for default/talk-worker-pool-1115. Supersedes the concurrency default and parser ceiling in 0010 and 0011. Their session isolation, uncapped waiting and exact T3-run ownership remain.

Task intake used to call JobBoard.pump synchronously. It held the board lock while preparing prompts, creating native sessions and admitting turns. A slow launch delayed the HTTP response, other launches and board controls. The parser also defaulted to one worker and refused settings above three.

The existing persisted Job records remain the FIFO queue. tasks.max_concurrent defaults to 3 and accepts any positive integer, rejecting booleans and nonintegers. Acceptance timestamps increase monotonically within the board so a frozen or backward clock cannot reorder newly accepted requests after restart. There is no waiting-count limit.

A lazy coordinator reserves RUNNING slots in FIFO order under the board lock. Each reserved job gets one worker thread for preparation, native launch and observation. External launch work runs outside the board lock. The first turn uses the conversation session; overlapping turns get independent sessions and cannot rebind the conversation session. The gateway opens its shared client connection once while allowing concurrent session RPCs.

A confirmed T3 admission removes the mini-task from slot accounting. Its existing follower still owns the exact thread/run and cannot deliver another run's result. The existing bound on still-live handed-off turns and their stop policy remains. Ending a voice call still does not cancel tasks.

Known process-capacity errors before a process exists return the same job to ACCEPTED, with a one-second retry delay. A failed worker-thread allocation restores the queued record. A permanent preparation or launch error settles FAILED and frees the slot. Cancellation during preparation prevents native admission, and cancellation during admission is acted on by the worker once its process or gateway receipt is known. If the OS cannot start the coordinator itself, intake retains the persisted request; a later wake or restart retries it. No new service, approval path, provider or core changes are introduced.

Hardware and deployment containment remain external constraints. This setting does not discover host capacity, reserve hardware or guarantee that a very large configured pool fits the host. Waiting requests remain durable when transient process capacity is exhausted.

Verification uses tests/capacity/run.py with the real HTTP server, board, NativeAgent and spool, and bounded synthetic native workers. It proves omitted-default three-worker concurrency, configured ten-worker concurrency with distinct PIDs and sessions, FIFO promotion, larger parser values without spawning them, responsive voice routes during slow startup, cancellation, restart adoption, deduplication, launch failures and resource retries. tests/work/run.py and the real Hermes agent integration retain exact T3-run and delivery coverage.
