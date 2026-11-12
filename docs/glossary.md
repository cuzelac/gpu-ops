# Glossary

Each term has one definition, and docs and tool output use the same wording every time.
If a definition here changes, update every doc that quotes it.

## gpu-ops terms

- **Job spec:** a TOML file in your project that says what to run, on what GPU, with which files, and the most it may cost.
- **Provider:** a company that rents GPUs, such as RunPod.
- **Adapter:** the gpu-ops code that talks to one provider's API.
- **Offer:** one rentable GPU option, with a type, a count, a tier and an hourly price.
- **Instance:** one rented machine running your container. RunPod calls it a pod.
- **Run:** one execution of a job spec, from creating the instance to deleting it.
- **Session:** one instance kept alive across several runs, so that you pay setup and teardown once.
- **Overhead:** the paid time in a run that is not your command: provisioning, bootstrap, upload, setup, download and teardown.
- **Run record:** the JSON file that says what a run did, what it cost and what it produced.
- **Ledger:** gpu-ops' local list of instances it believes are alive.
- **Dead-man timer:** a timer on the instance that deletes the instance at its deadline even if your PC is off.
- **Reaper:** a command that deletes any gpu-ops instance past its deadline.
- **Policy:** your user-level limits (per job, per month, allowed providers) that a job spec can tighten but never loosen.
- **Teardown:** deleting the instance and confirming with the provider that it is gone.
- **Manifest:** a list of files with their SHA-256 hashes, used to prove what arrived is what was sent.
- **Image digest:** the `sha256:…` fingerprint of one exact container image; unlike a tag such as `latest`, it can never point at different contents later.

## GPU rental terms

- **VRAM**: the GPU's own memory; the model and its working data must fit in it.
- **On-demand**: you rent a GPU at a fixed hourly price, and nobody can take it from you while it runs.
- **Spot / interruptible**: a cheaper rental the provider can take back at any moment, stopping your job.
- **Secure cloud**: GPUs in provider-vetted data centres.
- **Community cloud**: GPUs in machines owned by third-party hosts, rented through the provider.
- **Egress / ingress**: data leaving / entering the provider's network; some providers charge per GB.
- **Container disk**: scratch disk that is wiped when the machine is stopped or deleted.
- **Network volume**: a disk that outlives any one machine, so model weights can be kept between jobs.

## ML terms

- **Batch inference**: running a model over many prompts in one go, as opposed to chatting with it live.
- **QLoRA**: fine-tuning a model stored in 4-bit form by training only a small add-on, the adapter.
- **Adapter (ML)**: the small set of trained weights that QLoRA produces; it is added to the unchanged base model.
- **Distillation**: a large "teacher" model writes answers, and a smaller "student" model is trained to imitate them.
- **Epoch**: one full pass over the training data.
- **Eval loss**: how wrong the model is on held-out examples it was not trained on; lower is better.
