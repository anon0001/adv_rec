# -*- coding: utf-8 -*-
import torch
from torch.autograd import Variable

from .utils.misc import pbar
from .utils.topology import Topology


def tile_ctx_dict(ctx_dict, idxs):
    """Returns dict of 3D tensors repeatedly indexed along the sample axis."""
    # 1st: tensor, 2nd optional mask
    return {
        k: (t[:, idxs], None if mask is None else mask[:, idxs])
        for k, (t, mask) in ctx_dict.items()
    }


def beam_search(models, data_loader, task_id=None, beam_size=12, max_len=200,
                lp_alpha=0., suppress_unk=False):
    """An efficient GPU implementation for beam-search algorithm.

    Arguments:
        models (list of Model): Model instance(s) derived from `nn.Module`
            defining a set of methods. See `models/nmt.py`.
        data_loader (DataLoader): A ``DataLoader`` instance.
        task_id (str, optional): For multi-output models, this selects
            the decoder. (Default: None)
        beam_size (int, optional): The size of the beam. (Default: 12)
        max_len (int, optional): Maximum target length to stop beam-search
            if <eos> is still not generated. (Default: 200)
        lp_alpha (float, optional): If > 0, applies Google's length-penalty
            normalization instead of simple length normalization.
            lp: ((5 + |Y|)^lp_alpha / (5 + 1)^lp_alpha)
        suppress_unk (bool, optional): If `True`, suppresses the log-prob
            of <unk> token.

    Returns:
        list:
            A list of hypotheses in surface form.
    """

    # This is the batch-size requested by the user but with sorted
    # batches, efficient batch-size will be <= max_batch_size
    max_batch_size = data_loader.batch_sampler.batch_size
    k = beam_size
    inf = -1000
    results = []
    enc_args = {}

    if task_id is None:
        # For classical models that have single encoder, decoder and
        # target vocabulary
        decs = [m.dec for m in models]
        f_inits = [dec.f_init for dec in decs]
        f_nexts = [dec.f_next for dec in decs]
        vocab = models[0].trg_vocab
    else:
        # A specific input-output topology has been requested
        task = Topology(task_id)
        enc_args['enc_ids'] = task.srcs
        # For new multi-target models: select the first target decoder
        decs = [m.get_decoder(task.first_trg) for m in models]
        # Get the necessary init() and next() methods
        f_inits = [dec.f_init for dec in decs]
        f_nexts = [dec.f_next for dec in decs]
        # Get the corresponding vocabulary for the first target
        vocab = models[0].vocabs[task.first_trg]

    # Common parts
    encoders = [m.encode for m in models]
    unk = vocab['<unk>']
    eos = vocab['<eos>']
    n_vocab = len(vocab)

    # Tensorized beam that will shrink and grow up to max_batch_size
    beam_storage = torch.zeros((max_len, max_batch_size, k)).long().cuda()
    mask = torch.arange(max_batch_size * k).long().cuda()
    nll_storage = torch.zeros(max_batch_size).cuda()

    for batch in pbar(data_loader, unit='batch'):
        # Send to GPU
        batch.to_gpu(volatile=True)

        # Always use the initial storage
        beam = beam_storage.narrow(1, 0, batch.size).zero_()

        # Mask to apply to pdxs.view(-1) to fix indices
        nk_mask = mask.narrow(0, 0, batch.size * k)

        # nll: batch_size x 1 (will get expanded further)
        nll = nll_storage.narrow(0, 0, batch.size).unsqueeze(1)

        # Tile indices to use in the loop to expand first dim
        tile = range(batch.size)

        # Encode source modalities
        ctx_dicts = [encode(batch, **enc_args) for encode in encoders]

        # Get initial decoder state (N*H)
        h_ts = [f_init(ctx_dict) for f_init, ctx_dict in zip(f_inits, ctx_dicts)]

        # Start with <bos> tokens
        # FIXME: idxs should not change except for informed <bos> embeddings
        idxs = models[0].get_bos(batch.size).cuda()

        for t in range(max_len):
            # Fetch embs for the next iteration (N*K, E)
            # y_ts = [m.dec.emb(Variable(idxs, volatile=True).cuda()) for m in models]
            y_t = Variable(idxs, volatile=True).cuda()

            # Select correct positions from source context
            ctx_dicts = [tile_ctx_dict(cd, tile) for cd in ctx_dicts]

            # Get log probabilities and next state
            # log_p: batch_size x vocab_size (t = 0)
            #        batch_size*beam_size x vocab_size (t > 0)
            log_ps, h_ts = zip(
                *[f_next(cd, dec.emb(y_t), h_t[tile]) for
                    f_next, dec, cd, h_t in zip(f_nexts, decs, ctx_dicts, h_ts)])

            # Do the actual averaging of log-probabilities
            log_p = sum(log_ps).data
            if suppress_unk:
                log_p[:, unk] = inf

            # Detect <eos>'d hyps
            idxs = (idxs == 2).nonzero()
            if idxs.numel():
                if idxs.numel() == batch.size * k:
                    break
                idxs.squeeze_(-1)
                # Unfavor all candidates
                log_p.index_fill_(0, idxs, inf)
                # Favor <eos> so that it gets selected
                log_p.view(-1).index_fill_(0, idxs * n_vocab + 2, 0)

            # Expand to 3D, cross-sum scores and reduce back to 2D
            # log_p: batch_size x vocab_size ( t = 0 )
            #   nll: batch_size x beam_size (x 1)
            # nll becomes: batch_size x beam_size*vocab_size here
            # Reduce (N, K*V) to k-best
            nll, beam[t] = nll.unsqueeze_(2).add(log_p.view(
                batch.size, -1, n_vocab)).view(batch.size, -1).topk(
                k, sorted=False, largest=True)

            # previous indices into the beam and current token indices
            pdxs = beam[t] / n_vocab
            beam[t].remainder_(n_vocab)
            idxs = beam[t].view(-1)

            # Compute correct previous indices
            # Mask is needed since we're in flattened regime
            tile = pdxs.view(-1) + (nk_mask / k) * (k if t else 1)

            if t > 0:
                # Permute all hypothesis history according to new order
                beam[:t] = beam[:t].gather(2, pdxs.repeat(t, 1, 1))

        # Put an explicit <eos> to make idxs_to_sent happy
        beam[max_len - 1] = eos

        # Find lengths by summing tokens not in (pad,bos,eos)
        lp = beam.gt(2).float().sum(0).clamp(min=1)

        if lp_alpha > 0.:
            lp = ((5 + lp)**lp_alpha) / 6**lp_alpha

        # Apply length normalization and get best hyps
        top_hyps = nll.div_(lp).topk(
            1, sorted=False, largest=True)[1].squeeze(1)

        # Get best hyp for each sample in the batch
        hyps = beam[:, range(batch.size), top_hyps].t().cpu()
        results.extend(vocab.list_of_idxs_to_sents(hyps.tolist()))

    # Recover order of the samples if necessary
    if getattr(data_loader.batch_sampler, 'store_indices', False):
        results = [results[i] for i, j in sorted(
            enumerate(data_loader.batch_sampler.orig_idxs), key=lambda k: k[1])]
    return results
