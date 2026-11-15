// Bounded kernel speculation. Read views reserve RAM up front so exhaustion
// never falls through to committed buffers or leaves a half-updated metadata view.
#define NSPEC 4
#define SPEC_DIRTY 64
#define SPEC_OUTPUT 8192
#define SPEC_RECORDS 64
#define SPEC_PIPES 32
#define SPEC_INODES 200

enum specstate { SPEC_FREE, SPEC_ACTIVE, SPEC_COMMITTING, SPEC_CANCELLED };
