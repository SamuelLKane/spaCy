# coding: utf8
from __future__ import unicode_literals

import numpy
from thinc.v2v import Model, Maxout, Softmax, Affine, ReLu
from thinc.i2v import HashEmbed, StaticVectors
from thinc.t2t import ExtractWindow, ParametricAttention
from thinc.t2v import Pooling, sum_pool, mean_pool
from thinc.misc import Residual
from thinc.misc import LayerNorm as LN
from thinc.misc import FeatureExtracter
from thinc.api import add, layerize, chain, clone, concatenate, with_flatten
from thinc.api import with_getitem, flatten_add_lengths
from thinc.api import uniqued, wrap, noop
from thinc.api import with_square_sequences
from thinc.linear.linear import LinearModel
from thinc.neural.ops import NumpyOps, CupyOps
from thinc.neural.util import get_array_module
from thinc.neural.optimizers import Adam

from thinc import describe
from thinc.describe import Dimension, Synapses, Biases, Gradient
from thinc.neural._classes.affine import _set_dimensions_if_needed
import thinc.extra.load_nlp

from .attrs import ID, ORTH, LOWER, NORM, PREFIX, SUFFIX, SHAPE
from .errors import Errors
from . import util

try:
    import torch.nn
    from thinc.extra.wrappers import PyTorchWrapperRNN
except ImportError:
    torch = None

VECTORS_KEY = "spacy_pretrained_vectors"


def cosine(vec1, vec2):
    xp = get_array_module(vec1)
    norm1 = xp.linalg.norm(vec1)
    norm2 = xp.linalg.norm(vec2)
    if norm1 == 0.0 or norm2 == 0.0:
        return 0
    else:
        return vec1.dot(vec2) / (norm1 * norm2)


def create_default_optimizer(ops, **cfg):
    learn_rate = util.env_opt("learn_rate", 0.001)
    beta1 = util.env_opt("optimizer_B1", 0.9)
    beta2 = util.env_opt("optimizer_B2", 0.999)
    eps = util.env_opt("optimizer_eps", 1e-8)
    L2 = util.env_opt("L2_penalty", 1e-6)
    max_grad_norm = util.env_opt("grad_norm_clip", 1.0)
    optimizer = Adam(ops, learn_rate, L2=L2, beta1=beta1, beta2=beta2, eps=eps)
    optimizer.max_grad_norm = max_grad_norm
    optimizer.device = ops.device
    return optimizer


@layerize
def _flatten_add_lengths(seqs, pad=0, drop=0.0):
    ops = Model.ops
    lengths = ops.asarray([len(seq) for seq in seqs], dtype="i")

    def finish_update(d_X, sgd=None):
        return ops.unflatten(d_X, lengths, pad=pad)

    X = ops.flatten(seqs, pad=pad)
    return (X, lengths), finish_update


def _zero_init(model):
    def _zero_init_impl(self, *args, **kwargs):
        self.W.fill(0)

    model.on_init_hooks.append(_zero_init_impl)
    if model.W is not None:
        model.W.fill(0.0)
    return model


@layerize
def _preprocess_doc(docs, drop=0.0):
    keys = [doc.to_array(LOWER) for doc in docs]
    # The dtype here matches what thinc is expecting -- which differs per
    # platform (by int definition). This should be fixed once the problem
    # is fixed on Thinc's side.
    lengths = numpy.array([arr.shape[0] for arr in keys], dtype=numpy.int_)
    keys = numpy.concatenate(keys)
    vals = numpy.zeros(keys.shape, dtype='f')
    return (keys, vals, lengths), None


def with_cpu(ops, model):
    """Wrap a model that should run on CPU, transferring inputs and outputs
    as necessary."""
    model.to_cpu()

    def with_cpu_forward(inputs, drop=0.):
        cpu_outputs, backprop = model.begin_update(_to_cpu(inputs), drop=drop)
        gpu_outputs = _to_device(ops, cpu_outputs)

        def with_cpu_backprop(d_outputs, sgd=None):
            cpu_d_outputs = _to_cpu(d_outputs)
            return backprop(cpu_d_outputs, sgd=sgd)

        return gpu_outputs, with_cpu_backprop

    return wrap(with_cpu_forward, model)


def _to_cpu(X):
    if isinstance(X, numpy.ndarray):
        return X
    elif isinstance(X, tuple):
        return tuple([_to_cpu(x) for x in X])
    elif isinstance(X, list):
        return [_to_cpu(x) for x in X]
    elif hasattr(X, 'get'):
        return X.get()
    else:
        return X


def _to_device(ops, X):
    if isinstance(X, tuple):
        return tuple([_to_device(ops, x) for x in X])
    elif isinstance(X, list):
        return [_to_device(ops, x) for x in X]
    else:
        return ops.asarray(X)


@layerize
def _preprocess_doc_bigrams(docs, drop=0.0):
    unigrams = [doc.to_array(LOWER) for doc in docs]
    ops = Model.ops
    bigrams = [ops.ngrams(2, doc_unis) for doc_unis in unigrams]
    keys = [ops.xp.concatenate(feats) for feats in zip(unigrams, bigrams)]
    keys, vals = zip(*[ops.xp.unique(k, return_counts=True) for k in keys])
    # The dtype here matches what thinc is expecting -- which differs per
    # platform (by int definition). This should be fixed once the problem
    # is fixed on Thinc's side.
    lengths = ops.asarray([arr.shape[0] for arr in keys], dtype=numpy.int_)
    keys = ops.xp.concatenate(keys)
    vals = ops.asarray(ops.xp.concatenate(vals), dtype="f")
    return (keys, vals, lengths), None


@describe.on_data(
    _set_dimensions_if_needed, lambda model, X, y: model.init_weights(model)
)
@describe.attributes(
    nI=Dimension("Input size"),
    nF=Dimension("Number of features"),
    nO=Dimension("Output size"),
    nP=Dimension("Maxout pieces"),
    W=Synapses("Weights matrix", lambda obj: (obj.nF, obj.nO, obj.nP, obj.nI)),
    b=Biases("Bias vector", lambda obj: (obj.nO, obj.nP)),
    pad=Synapses(
        "Pad",
        lambda obj: (1, obj.nF, obj.nO, obj.nP),
        lambda M, ops: ops.normal_init(M, 1.0),
    ),
    d_W=Gradient("W"),
    d_pad=Gradient("pad"),
    d_b=Gradient("b"),
)
class PrecomputableAffine(Model):
    def __init__(self, nO=None, nI=None, nF=None, nP=None, **kwargs):
        Model.__init__(self, **kwargs)
        self.nO = nO
        self.nP = nP
        self.nI = nI
        self.nF = nF

    def begin_update(self, X, drop=0.0):
        Yf = self.ops.gemm(
            X, self.W.reshape((self.nF * self.nO * self.nP, self.nI)), trans2=True
        )
        Yf = Yf.reshape((Yf.shape[0], self.nF, self.nO, self.nP))
        Yf = self._add_padding(Yf)

        def backward(dY_ids, sgd=None):
            dY, ids = dY_ids
            dY, ids = self._backprop_padding(dY, ids)
            Xf = X[ids]
            Xf = Xf.reshape((Xf.shape[0], self.nF * self.nI))

            self.d_b += dY.sum(axis=0)
            dY = dY.reshape((dY.shape[0], self.nO * self.nP))

            Wopfi = self.W.transpose((1, 2, 0, 3))
            Wopfi = self.ops.xp.ascontiguousarray(Wopfi)
            Wopfi = Wopfi.reshape((self.nO * self.nP, self.nF * self.nI))
            dXf = self.ops.gemm(dY.reshape((dY.shape[0], self.nO * self.nP)), Wopfi)

            # Reuse the buffer
            dWopfi = Wopfi
            dWopfi.fill(0.0)
            self.ops.gemm(dY, Xf, out=dWopfi, trans1=True)
            dWopfi = dWopfi.reshape((self.nO, self.nP, self.nF, self.nI))
            # (o, p, f, i) --> (f, o, p, i)
            self.d_W += dWopfi.transpose((2, 0, 1, 3))

            if sgd is not None:
                sgd(self._mem.weights, self._mem.gradient, key=self.id)
            return dXf.reshape((dXf.shape[0], self.nF, self.nI))

        return Yf, backward

    def _add_padding(self, Yf):
        Yf_padded = self.ops.xp.vstack((self.pad, Yf))
        return Yf_padded

    def _backprop_padding(self, dY, ids):
        # (1, nF, nO, nP) += (nN, nF, nO, nP) where IDs (nN, nF) < 0
        mask = ids < 0.0
        mask = mask.sum(axis=1)
        d_pad = dY * mask.reshape((ids.shape[0], 1, 1))
        self.d_pad += d_pad.sum(axis=0)
        return dY, ids

    @staticmethod
    def init_weights(model):
        """This is like the 'layer sequential unit variance', but instead
        of taking the actual inputs, we randomly generate whitened data.

        Why's this all so complicated? We have a huge number of inputs,
        and the maxout unit makes guessing the dynamics tricky. Instead
        we set the maxout weights to values that empirically result in
        whitened outputs given whitened inputs.
        """
        if (model.W ** 2).sum() != 0.0:
            return
        ops = model.ops
        xp = ops.xp
        ops.normal_init(model.W, model.nF * model.nI, inplace=True)

        ids = ops.allocate((5000, model.nF), dtype="f")
        ids += xp.random.uniform(0, 1000, ids.shape)
        ids = ops.asarray(ids, dtype="i")
        tokvecs = ops.allocate((5000, model.nI), dtype="f")
        tokvecs += xp.random.normal(loc=0.0, scale=1.0, size=tokvecs.size).reshape(
            tokvecs.shape
        )

        def predict(ids, tokvecs):
            # nS ids. nW tokvecs. Exclude the padding array.
            hiddens = model(tokvecs[:-1])  # (nW, f, o, p)
            vectors = model.ops.allocate((ids.shape[0], model.nO * model.nP), dtype="f")
            # need nS vectors
            hiddens = hiddens.reshape(
                (hiddens.shape[0] * model.nF, model.nO * model.nP)
            )
            model.ops.scatter_add(vectors, ids.flatten(), hiddens)
            vectors = vectors.reshape((vectors.shape[0], model.nO, model.nP))
            vectors += model.b
            vectors = model.ops.asarray(vectors)
            if model.nP >= 2:
                return model.ops.maxout(vectors)[0]
            else:
                return vectors * (vectors >= 0)

        tol_var = 0.01
        tol_mean = 0.01
        t_max = 10
        t_i = 0
        for t_i in range(t_max):
            acts1 = predict(ids, tokvecs)
            var = model.ops.xp.var(acts1)
            mean = model.ops.xp.mean(acts1)
            if abs(var - 1.0) >= tol_var:
                model.W /= model.ops.xp.sqrt(var)
            elif abs(mean) >= tol_mean:
                model.b -= mean
            else:
                break


def link_vectors_to_models(vocab):
    vectors = vocab.vectors
    if vectors.name is None:
        vectors.name = VECTORS_KEY
        if vectors.data.size != 0:
            print(
                "Warning: Unnamed vectors -- this won't allow multiple vectors "
                "models to be loaded. (Shape: (%d, %d))" % vectors.data.shape
            )
    ops = Model.ops
    for word in vocab:
        if word.orth in vectors.key2row:
            word.rank = vectors.key2row[word.orth]
        else:
            word.rank = 0
    data = ops.asarray(vectors.data)
    # Set an entry here, so that vectors are accessed by StaticVectors
    # (unideal, I know)
    thinc.extra.load_nlp.VECTORS[(ops.device, vectors.name)] = data


def PyTorchBiLSTM(nO, nI, depth, dropout=0.2):
    if depth == 0:
        return layerize(noop())
    model = torch.nn.LSTM(nI, nO // 2, depth, bidirectional=True, dropout=dropout)
    return with_square_sequences(PyTorchWrapperRNN(model))


def Tok2Vec(width, embed_size, **kwargs):
    pretrained_vectors = kwargs.get("pretrained_vectors", None)
    cnn_maxout_pieces = kwargs.get("cnn_maxout_pieces", 3)
    subword_features = kwargs.get("subword_features", True)
    conv_depth = kwargs.get("conv_depth", 4)
    bilstm_depth = kwargs.get("bilstm_depth", 0)
    cols = [ID, NORM, PREFIX, SUFFIX, SHAPE, ORTH]
    with Model.define_operators(
        {">>": chain, "|": concatenate, "**": clone, "+": add, "*": reapply}
    ):
        norm = HashEmbed(width, embed_size, column=cols.index(NORM), name="embed_norm")
        if subword_features:
            prefix = HashEmbed(
                width, embed_size // 2, column=cols.index(PREFIX), name="embed_prefix"
            )
            suffix = HashEmbed(
                width, embed_size // 2, column=cols.index(SUFFIX), name="embed_suffix"
            )
            shape = HashEmbed(
                width, embed_size // 2, column=cols.index(SHAPE), name="embed_shape"
            )
        else:
            prefix, suffix, shape = (None, None, None)
        if pretrained_vectors is not None:
            glove = StaticVectors(pretrained_vectors, width, column=cols.index(ID))

            if subword_features:
                embed = uniqued(
                    (glove | norm | prefix | suffix | shape)
                    >> LN(Maxout(width, width * 5, pieces=3)),
                    column=cols.index(ORTH),
                )
            else:
                embed = uniqued(
                    (glove | norm) >> LN(Maxout(width, width * 2, pieces=3)),
                    column=cols.index(ORTH),
                )
        elif subword_features:
            embed = uniqued(
                (norm | prefix | suffix | shape)
                >> LN(Maxout(width, width * 4, pieces=3)),
                column=cols.index(ORTH),
            )
        else:
            embed = norm

        convolution = Residual(
            ExtractWindow(nW=1)
            >> LN(Maxout(width, width * 3, pieces=cnn_maxout_pieces))
        )
        tok2vec = FeatureExtracter(cols) >> with_flatten(
            embed >> convolution ** conv_depth, pad=conv_depth
        )
        if bilstm_depth >= 1:
            tok2vec = tok2vec >> PyTorchBiLSTM(width, width, bilstm_depth)
        # Work around thinc API limitations :(. TODO: Revise in Thinc 7
        tok2vec.nO = width
        tok2vec.embed = embed
    return tok2vec


def reapply(layer, n_times):
    def reapply_fwd(X, drop=0.0):
        backprops = []
        for i in range(n_times):
            Y, backprop = layer.begin_update(X, drop=drop)
            X = Y
            backprops.append(backprop)

        def reapply_bwd(dY, sgd=None):
            dX = None
            for backprop in reversed(backprops):
                dY = backprop(dY, sgd=sgd)
                if dX is None:
                    dX = dY
                else:
                    dX += dY
            return dX

        return Y, reapply_bwd

    return wrap(reapply_fwd, layer)


def asarray(ops, dtype):
    def forward(X, drop=0.0):
        return ops.asarray(X, dtype=dtype), None

    return layerize(forward)


def _divide_array(X, size):
    parts = []
    index = 0
    while index < len(X):
        parts.append(X[index : index + size])
        index += size
    return parts


def get_col(idx):
    if idx < 0:
        raise IndexError(Errors.E066.format(value=idx))

    def forward(X, drop=0.0):
        if isinstance(X, numpy.ndarray):
            ops = NumpyOps()
        else:
            ops = CupyOps()
        output = ops.xp.ascontiguousarray(X[:, idx], dtype=X.dtype)

        def backward(y, sgd=None):
            dX = ops.allocate(X.shape)
            dX[:, idx] += y
            return dX

        return output, backward

    return layerize(forward)


def doc2feats(cols=None):
    if cols is None:
        cols = [ID, NORM, PREFIX, SUFFIX, SHAPE, ORTH]

    def forward(docs, drop=0.0):
        feats = []
        for doc in docs:
            feats.append(doc.to_array(cols))
        return feats, None

    model = layerize(forward)
    model.cols = cols
    return model


def print_shape(prefix):
    def forward(X, drop=0.0):
        return X, lambda dX, **kwargs: dX

    return layerize(forward)


@layerize
def get_token_vectors(tokens_attrs_vectors, drop=0.0):
    tokens, attrs, vectors = tokens_attrs_vectors

    def backward(d_output, sgd=None):
        return (tokens, d_output)

    return vectors, backward


@layerize
def logistic(X, drop=0.0):
    xp = get_array_module(X)
    if not isinstance(X, xp.ndarray):
        X = xp.asarray(X)
    # Clip to range (-10, 10)
    X = xp.minimum(X, 10.0, X)
    X = xp.maximum(X, -10.0, X)
    Y = 1.0 / (1.0 + xp.exp(-X))

    def logistic_bwd(dY, sgd=None):
        dX = dY * (Y * (1 - Y))
        return dX

    return Y, logistic_bwd


def zero_init(model):
    def _zero_init_impl(self, X, y):
        self.W.fill(0)

    model.on_data_hooks.append(_zero_init_impl)
    return model


@layerize
def preprocess_doc(docs, drop=0.0):
    keys = [doc.to_array([LOWER]) for doc in docs]
    ops = Model.ops
    lengths = ops.asarray([arr.shape[0] for arr in keys])
    keys = ops.xp.concatenate(keys)
    vals = ops.allocate(keys.shape[0]) + 1
    return (keys, vals, lengths), None


def getitem(i):
    def getitem_fwd(X, drop=0.0):
        return X[i], None

    return layerize(getitem_fwd)


def build_tagger_model(nr_class, **cfg):
    embed_size = util.env_opt("embed_size", 2000)
    if "token_vector_width" in cfg:
        token_vector_width = cfg["token_vector_width"]
    else:
        token_vector_width = util.env_opt("token_vector_width", 96)
    pretrained_vectors = cfg.get("pretrained_vectors")
    subword_features = cfg.get("subword_features", True)
    with Model.define_operators({">>": chain, "+": add}):
        if "tok2vec" in cfg:
            tok2vec = cfg["tok2vec"]
        else:
            tok2vec = Tok2Vec(
                token_vector_width,
                embed_size,
                subword_features=subword_features,
                pretrained_vectors=pretrained_vectors,
            )
        softmax = with_flatten(Softmax(nr_class, token_vector_width))
        model = tok2vec >> softmax
    model.nI = None
    model.tok2vec = tok2vec
    model.softmax = softmax
    return model


@layerize
def SpacyVectors(docs, drop=0.0):
    batch = []
    for doc in docs:
        indices = numpy.zeros((len(doc),), dtype="i")
        for i, word in enumerate(doc):
            if word.orth in doc.vocab.vectors.key2row:
                indices[i] = doc.vocab.vectors.key2row[word.orth]
            else:
                indices[i] = 0
        vectors = doc.vocab.vectors.data[indices]
        batch.append(vectors)
    return batch, None


def build_text_classifier(nr_class, width=64, **cfg):
    depth = cfg.get("depth", 2)
    nr_vector = cfg.get("nr_vector", 5000)
    pretrained_dims = cfg.get("pretrained_dims", 0)
    with Model.define_operators({">>": chain, "+": add, "|": concatenate, "**": clone}):
        if cfg.get("low_data") and pretrained_dims:
            model = (
                SpacyVectors
                >> flatten_add_lengths
                >> with_getitem(0, Affine(width, pretrained_dims))
                >> ParametricAttention(width)
                >> Pooling(sum_pool)
                >> Residual(ReLu(width, width)) ** 2
                >> zero_init(Affine(nr_class, width, drop_factor=0.0))
                >> logistic
            )
            return model

        lower = HashEmbed(width, nr_vector, column=1)
        prefix = HashEmbed(width // 2, nr_vector, column=2)
        suffix = HashEmbed(width // 2, nr_vector, column=3)
        shape = HashEmbed(width // 2, nr_vector, column=4)

        trained_vectors = FeatureExtracter(
            [ORTH, LOWER, PREFIX, SUFFIX, SHAPE, ID]
        ) >> with_flatten(
            uniqued(
                (lower | prefix | suffix | shape)
                >> LN(Maxout(width, width + (width // 2) * 3)),
                column=0,
            )
        )

        if pretrained_dims:
            static_vectors = SpacyVectors >> with_flatten(
                Affine(width, pretrained_dims)
            )
            # TODO Make concatenate support lists
            vectors = concatenate_lists(trained_vectors, static_vectors)
            vectors_width = width * 2
        else:
            vectors = trained_vectors
            vectors_width = width
            static_vectors = None
        tok2vec = vectors >> with_flatten(
            LN(Maxout(width, vectors_width))
            >> Residual((ExtractWindow(nW=1) >> LN(Maxout(width, width * 3)))) ** depth,
            pad=depth,
        )
        cnn_model = (
            tok2vec
            >> flatten_add_lengths
            >> ParametricAttention(width)
            >> Pooling(sum_pool)
            >> Residual(zero_init(Maxout(width, width)))
            >> zero_init(Affine(nr_class, width, drop_factor=0.0))
        )

        linear_model = (
            _preprocess_doc
            >> with_cpu(Model.ops, LinearModel(nr_class))
        )
        if cfg.get('exclusive_classes'):
            output_layer = Softmax(nr_class, nr_class * 2)
        else:
            output_layer = (
                zero_init(Affine(nr_class, nr_class * 2, drop_factor=0.0))
                >> logistic
            )
        model = (
            (linear_model | cnn_model)
            >> output_layer
        )
        model.tok2vec = chain(tok2vec, flatten)
    model.nO = nr_class
    model.lsuv = False
    return model


def build_simple_cnn_text_classifier(tok2vec, nr_class, exclusive_classes=False, **cfg):
    """
    Build a simple CNN text classifier, given a token-to-vector model as inputs.
    If exclusive_classes=True, a softmax non-linearity is applied, so that the
    outputs sum to 1. If exclusive_classes=False, a logistic non-linearity
    is applied instead, so that outputs are in the range [0, 1].
    """
    with Model.define_operators({">>": chain}):
        if exclusive_classes:
            output_layer = Softmax(nr_class, tok2vec.nO)
        else:
            output_layer = zero_init(Affine(nr_class, tok2vec.nO, drop_factor=0.0)) >> logistic
        model = tok2vec >> flatten_add_lengths >> Pooling(mean_pool) >> output_layer
    model.tok2vec = chain(tok2vec, flatten)
    model.nO = nr_class
    return model


@layerize
def flatten(seqs, drop=0.0):
    ops = Model.ops
    lengths = ops.asarray([len(seq) for seq in seqs], dtype="i")

    def finish_update(d_X, sgd=None):
        return ops.unflatten(d_X, lengths, pad=0)

    X = ops.flatten(seqs, pad=0)
    return X, finish_update


def concatenate_lists(*layers, **kwargs):  # pragma: no cover
    """Compose two or more models `f`, `g`, etc, such that their outputs are
    concatenated, i.e. `concatenate(f, g)(x)` computes `hstack(f(x), g(x))`
    """
    if not layers:
        return noop()
    drop_factor = kwargs.get("drop_factor", 1.0)
    ops = layers[0].ops
    layers = [chain(layer, flatten) for layer in layers]
    concat = concatenate(*layers)

    def concatenate_lists_fwd(Xs, drop=0.0):
        drop *= drop_factor
        lengths = ops.asarray([len(X) for X in Xs], dtype="i")
        flat_y, bp_flat_y = concat.begin_update(Xs, drop=drop)
        ys = ops.unflatten(flat_y, lengths)

        def concatenate_lists_bwd(d_ys, sgd=None):
            return bp_flat_y(ops.flatten(d_ys), sgd=sgd)

        return ys, concatenate_lists_bwd

    model = wrap(concatenate_lists_fwd, concat)
    return model


def masked_language_model(vocab, model, mask_prob=0.15):
    """Convert a model into a BERT-style masked language model"""

    random_words = _RandomWords(vocab)

    def mlm_forward(docs, drop=0.0):
        mask, docs = _apply_mask(docs, random_words, mask_prob=mask_prob)
        mask = model.ops.asarray(mask).reshape((mask.shape[0], 1))
        output, backprop = model.begin_update(docs, drop=drop)

        def mlm_backward(d_output, sgd=None):
            d_output *= 1 - mask
            return backprop(d_output, sgd=sgd)

        return output, mlm_backward

    return wrap(mlm_forward, model)


class _RandomWords(object):
    def __init__(self, vocab):
        self.words = [lex.text for lex in vocab if lex.prob != 0.0]
        self.probs = [lex.prob for lex in vocab if lex.prob != 0.0]
        self.words = self.words[:10000]
        self.probs = self.probs[:10000]
        self.probs = numpy.exp(numpy.array(self.probs, dtype="f"))
        self.probs /= self.probs.sum()
        self._cache = []

    def next(self):
        if not self._cache:
            self._cache.extend(
                numpy.random.choice(len(self.words), 10000, p=self.probs)
            )
        index = self._cache.pop()
        return self.words[index]


def _apply_mask(docs, random_words, mask_prob=0.15):
    # This needs to be here to avoid circular imports
    from .tokens.doc import Doc

    N = sum(len(doc) for doc in docs)
    mask = numpy.random.uniform(0.0, 1.0, (N,))
    mask = mask >= mask_prob
    i = 0
    masked_docs = []
    for doc in docs:
        words = []
        for token in doc:
            if not mask[i]:
                word = _replace_word(token.text, random_words)
            else:
                word = token.text
            words.append(word)
            i += 1
        spaces = [bool(w.whitespace_) for w in doc]
        # NB: If you change this implementation to instead modify
        # the docs in place, take care that the IDs reflect the original
        # words. Currently we use the original docs to make the vectors
        # for the target, so we don't lose the original tokens. But if
        # you modified the docs in place here, you would.
        masked_docs.append(Doc(doc.vocab, words=words, spaces=spaces))
    return mask, masked_docs


def _replace_word(word, random_words, mask="[MASK]"):
    roll = numpy.random.random()
    if roll < 0.8:
        return mask
    elif roll < 0.9:
        return random_words.next()
    else:
        return word
