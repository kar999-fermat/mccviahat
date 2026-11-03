FROM ubuntu:24.04

RUN apt-get update && apt-get install -y \
    build-essential \
    cmake \
    git \
    curl \
 && rm -rf /var/lib/apt/lists/*

# Pin llama.cpp so every node runs the same inference code, whatever day it
# was built. Change it on purpose with:
#   LLAMA_CPP_REF=<tag-or-commit> bash scripts/model/llama_build.sh
ARG LLAMA_CPP_REF=b12060
LABEL llama_cpp_ref=$LLAMA_CPP_REF

WORKDIR /opt
RUN git clone https://github.com/ggml-org/llama.cpp.git \
 && cd llama.cpp \
 && git checkout "$LLAMA_CPP_REF" \
 && git rev-parse HEAD > /opt/llama.cpp/COMMIT

WORKDIR /opt/llama.cpp
RUN cmake -B build -DLLAMA_BUILD_SERVER=ON \
 && cmake --build build -j

WORKDIR /models
EXPOSE 8000

# Default model path (override at runtime with -e MODEL_PATH=...)
ENV MODEL_PATH=/models/model.gguf

# Shell form so $MODEL_PATH expands at container runtime
CMD /opt/llama.cpp/build/bin/llama-server --host 0.0.0.0 --port 8000 --model $MODEL_PATH
