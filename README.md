# Yosys performance test suite

Runtime and memory usage test collection for [Yosys](https://github.com/YosysHQ/yosys). Very early stage repo. Might change a lot soon.

## Directory structure

+ thing.py
  - the script you run to get things done
+ scripts
  - Yosys synthesis scripts
+ artifacts
  - isolated, pre-processed files, derived from canonical design sources
+ canon
  - canonical design sources
  - covered by third-party licenses

## Licensing

Files outside of `artifacts` and `canon` are covered under the MIT license. See the `LICENSE` file.

Files in `artifacts` and `canon` are covered by various licenses which can be found in `canon` subdirectories. Files in `artifacts` have a clear correspondence to third-party files in `canon` that they're derived from. We redistribute them on the legal basis of redistributing the original third-party source file licenses and consider them modified versions of these source files

## Testing performance on artifacts

TODO

## Regenerating artifacts

When changing the `canon` directory, for example by bumping submodules or adding vendored third-party designs, it is necessary to re-generate `artifacts`.

For transparency, users are encouraged to regenerate them as well. If a code diff is then created against checked-in `artifacts`, and `canon/ys-version` hasn't changed (specifically the git commit it mentions), this is a bug in transparency or reproducibility, please report it.
