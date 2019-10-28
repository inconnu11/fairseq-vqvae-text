from fairseq import options, utils
from fairseq.models import (
    FairseqLanguageModel,
    register_model,
    register_model_architecture,
)
from fairseq.models.typed_transformer import (
    Embedding,
    TransformerEncoder,
    TransformerDecoder,
)

import torch
from torch.nn import functional as F
from torch import nn

DEFAULT_MAX_SOURCE_POSITIONS = 1024
DEFAULT_MAX_TARGET_POSITIONS = 1024


def print_stats(stats):
    for k, v in stats.items():
        print("{} = {}".format(k, v.item()))


def parse_kernel_and_strides(kernel, stride):
    def _parse(inpt):
        return list(map(int, inpt.split(",")))

    return _parse(kernel), _parse(stride)


def compute_conv_mask(lengths, stride):
    # lengths: B
    # we use odd-number kernel
    valid_lengths = (lengths - 1) / stride + 1
    max_length = torch.max(valid_lengths).item()
    mask = torch.arange(max_length, device=lengths.device).type_as(lengths).expand(len(lengths), max_length)
    mask = mask < valid_lengths.unsqueeze(1)
    return valid_lengths, mask  # mask -> batch x T'


class ConvEncoder(nn.Module):
    def __init__(self, input_channel, kernels, strides, latent_dim):
        super().__init__()
        self.strides = strides
        self.conv_blocks = nn.ModuleList([])
        self.conv_blocks.extend([nn.Conv1d(input_channel, input_channel, kernel_size=k, padding=k//2, stride=s)
                                 for k, s in zip(kernels, strides)])
        self.quant_conv = nn.Conv1d(input_channel, latent_dim, 1)

    def forward(self, input, lengths):
        # input: batch x C x T
        new_mask = None
        for ii, (conv_layer, s) in enumerate(zip(self.conv_blocks, self.strides)):
            input = F.relu(conv_layer(input))
            lengths, new_mask = compute_conv_mask(lengths, s)
            input = input * new_mask.type_as(input).unsqueeze(1)
        output = self.quant_conv(input)
        # output: batch x C' x T' -> T' x batch x C'
        # new_mask: batch x T'
        return output.permute(2, 0, 1), new_mask


class Quantize(nn.Module):
    def __init__(self, dim, n_embed, decay=0.99, eps=1e-5):
        super().__init__()

        self.dim = dim
        self.n_embed = n_embed
        self.decay = decay
        self.eps = eps

        embed = torch.randn(dim, n_embed)
        self.register_buffer('embed', embed)
        self.register_buffer('cluster_size', torch.zeros(n_embed))
        self.register_buffer('embed_avg', embed.clone())

    def forward(self, input, input_mask):
        '''
        :param input: T x batch x C, number of channels: dimension C
        :param input_mask: T x batch
        :return:
        '''
        # S = T x C
        flatten = input.reshape(-1, self.dim)  # S x C
        dist = (
            flatten.pow(2).sum(1, keepdim=True)  # S x 1
            - 2 * flatten @ self.embed   # S x C @ C x S
            + self.embed.pow(2).sum(0, keepdim=True)  # 1 x S
        )
        _, embed_ind = (-dist).max(1)  # S
        embed_onehot = F.one_hot(embed_ind, self.n_embed).type(
            flatten.dtype)  # S x K

        embed_ind = embed_ind.view(*input.shape[:-1])  # T x batch
        quantize = self.embed_code(embed_ind)  # T X batch x C

        # todo: this is for debugging, comment it later
        stats = {}
        # batch = embed_ind.size(1)
        # lengths = torch.sum(input_mask, dim=0)
        # avg = input.new_zeros((batch))
        # masked_embed_inds = embed_ind * input_mask.type_as(embed_ind) + torch.ones_like(embed_ind) * -1 * (1 - input_mask.type_as(embed_ind))
        # for ii in range(batch):
        #     avg[ii] = len(torch.unique(masked_embed_inds[:, ii])) - 1
        # tot_unique_per_batch = len(torch.unique(masked_embed_inds)) - 1
        # avg_unique_per_example = torch.sum(avg / lengths.type_as(avg)) / batch
        # stats['tot unique latents per batch'] = tot_unique_per_batch
        # stats['avg unique latents per example'] = avg_unique_per_example

        effective_units = 1.0 / embed_onehot[input_mask.view(-1)].mean(0).pow(2).sum()
        stats['effective latents per batch'] = effective_units
        if self.training:
            unmasked_flatten = torch.masked_select(flatten, input_mask.view(-1, 1)).contiguous().view(-1, self.dim)  # num_latents x C
            unmasked_embed_onehot = torch.masked_select(embed_onehot, input_mask.view(-1, 1)).contiguous().view(-1, self.n_embed)  # num_latents x K

            cluster_sum = unmasked_embed_onehot.sum(0)
            embed_sum = unmasked_flatten.transpose(0, 1) @ unmasked_embed_onehot  # C x K

            if torch.cuda.device_count() > 1:
                torch.distributed.all_reduce(cluster_sum)
                torch.distributed.all_reduce(embed_sum)

            self.cluster_size.data.mul_(self.decay).add_(
                1 - self.decay, cluster_sum
            )
            self.embed_avg.data.mul_(self.decay).add_(
                1 - self.decay, embed_sum
            )
            n = self.cluster_size.sum()
            cluster_size = (
                (self.cluster_size + self.eps) / (n + self.n_embed * self.eps) * n
            )
            embed_normalized = self.embed_avg / cluster_size.unsqueeze(0)
            self.embed.data.copy_(embed_normalized)

            stats['moving_avg_cluster_mean'] = torch.mean(self.cluster_size)
            stats['moving_avg_cluster_var'] = torch.var(self.cluster_size)

        input_mask = input_mask.type_as(input)
        quantize = quantize * input_mask.unsqueeze(-1)
        input = input * input_mask.unsqueeze(-1)
        diff = (quantize.detach() - input).pow(2).mean()
        quantize = input + (quantize - input).detach()

        return quantize, diff, embed_ind, stats

    def embed_code(self, embed_id):
        return F.embedding(embed_id, self.embed.transpose(0, 1))


@register_model('vqvae_lm')
class VQVAE(FairseqLanguageModel):
    def __init__(self, args, text_encoder, text_conv_encoder, text_decoder, bottom_quantizer, bottom_latent_encoder):
        super().__init__(text_decoder)
        self.text_encoder = text_encoder
        self.text_conv_encoder = text_conv_encoder
        self.bottom_quantizer = bottom_quantizer
        self.bottom_latent_encoder = bottom_latent_encoder

        self.bottom_conv_kernel_size, self.bottom_conv_strides = \
            parse_kernel_and_strides(args.bottom_conv_kernel_size, args.bottom_conv_stride)

        self.pad_index = text_decoder.padding_idx
        self.word_drop_rate = args.drop_word_prob
        self.pretrain_steps = args.pretrain_steps

    @staticmethod
    def add_args(parser):
        """Add model-specific arguments to the parser."""
        # fmt: off

        ### general arguments for all components
        parser.add_argument('--activation-fn',
                            choices=utils.get_available_activation_fns(),
                            help='activation function to use')
        parser.add_argument('--dropout', type=float, metavar='D',
                            help='dropout probability')
        parser.add_argument('--attention-dropout', type=float, metavar='D',
                            help='dropout probability for attention weights')
        parser.add_argument('--activation-dropout', '--relu-dropout', type=float, metavar='D',
                            help='dropout probability after activation in FFN.')
        parser.add_argument('--encoder-normalize-before', action='store_true',
                            help='apply layernorm before each encoder block')
        parser.add_argument('--encoder-learned-pos', action='store_true',
                            help='use learned positional embeddings in the encoder')
        parser.add_argument('--decoder-learned-pos', action='store_true',
                            help='use learned positional embeddings in the decoder')
        parser.add_argument('--decoder-normalize-before', action='store_true',
                            help='apply layernorm before each decoder block')

        parser.add_argument('--share-all-embeddings', action='store_true',
                            help='share encoder, decoder and output embeddings'
                                 ' (requires shared dictionary and embed dim)')
        parser.add_argument('--no-token-positional-embeddings', default=False, action='store_true',
                            help='if set, disables positional embeddings (outside self attention)')
        parser.add_argument('--adaptive-softmax-cutoff', metavar='EXPR',
                            help='comma separated list of adaptive softmax cutoff points. '
                                 'Must be used with adaptive_loss criterion'),
        parser.add_argument('--adaptive-softmax-dropout', type=float, metavar='D',
                            help='sets adaptive softmax dropout for the tail projections')
        # args for "Cross+Self-Attention for Transformer Models" (Peitz et al., 2019)
        parser.add_argument('--no-cross-attention', default=False, action='store_true',
                            help='do not perform cross-attention')
        parser.add_argument('--cross-self-attention', default=False, action='store_true',
                            help='perform cross+self-attention')
        parser.add_argument('--layer-wise-attention', default=False, action='store_true',
                            help='perform layer-wise attention (cross-attention or cross+self-attention)')

        # TODO: add pretrained latent embedding path
        parser.add_argument('--encoder-embed-path', type=str, metavar='STR',
                            help='path to pre-trained encoder embedding')
        parser.add_argument('--decoder-embed-path', type=str, metavar='STR',
                            help='path to pre-trained decoder embedding')

        # arguments for text encoder (transformer)
        parser.add_argument('--encoder-embed-dim', type=int, metavar='N',
                            help='encoder embedding dimension')
        parser.add_argument('--encoder-ffn-embed-dim', type=int, metavar='N',
                            help='encoder embedding dimension for FFN')
        parser.add_argument('--encoder-layers', type=int, metavar='N',
                            help='num encoder layers')
        parser.add_argument('--encoder-attention-heads', type=int, metavar='N',
                            help='num encoder attention heads')

        # arguments for bottom level convolutional text encoder
        parser.add_argument('--bottom-conv-kernel-size', type=str,
                            help='[format: 1,2,3], kernel sizes for the bottom latent variables conv layers, '
                                 'with transformer encoder outputs as inputs')
        parser.add_argument("--bottom-conv-stride", type=str,
                            help='[format: 2,2,2], strides for the bottom latent variables conv layers')

        # arguments for the bottom level quantization
        parser.add_argument('--bottom-latent-dim', type=int,
                            help='bottom code book dimension')
        parser.add_argument('--bottom-latent-k', type=int,
                            help='bottom code book size')

        # todo: condition (attention) on the top level discrete representations and add condition to the typed TransformerEncoderLayer
        # arguments for the bottom level discrete latent variable encoder (transformer)
        #--bottom-encoder-embed-dim = --bottom-latent-dim
        parser.add_argument('--use-bottom-quantants-encoder', type=int, metavar='N')
        parser.add_argument('--bottom-encoder-ffn-embed-dim', type=int, metavar='N',
                            help='encoder embedding dimension for FFN')
        parser.add_argument('--bottom-encoder-layers', type=int, metavar='N',
                            help='num encoder layers')
        parser.add_argument('--bottom-encoder-attention-heads', type=int, metavar='N',
                            help='num encoder attention heads')

        # arguments for text decoder (transformer)
        parser.add_argument('--decoder-embed-dim', type=int, metavar='N',
                            help='decoder embedding dimension')
        parser.add_argument('--decoder-ffn-embed-dim', type=int, metavar='N',
                            help='decoder embedding dimension for FFN')
        parser.add_argument('--decoder-layers', type=int, metavar='N',
                            help='num decoder layers')
        parser.add_argument('--decoder-attention-heads', type=int, metavar='N',
                            help='num decoder attention heads')
        parser.add_argument('--share-decoder-input-output-embed', action='store_true',
                            help='share decoder input and output embeddings')

        parser.add_argument('--drop-word-prob', type=float,
                            help='probability of drop words of decoder inputs')
        # ablations
        parser.add_argument('--pretrain-steps', type=int, metavar='N')
        parser.add_argument('--use-latent', type=int, metavar='N')

        # fmt: on

    @classmethod
    def build_model(cls, args, task):
        """Build a new model instance."""

        # make sure all arguments are present in older models
        base_architecture(args)

        if not hasattr(args, 'max_source_positions'):
            args.max_source_positions = DEFAULT_MAX_SOURCE_POSITIONS

        src_dict = task.source_dictionary

        def build_embedding(dictionary, embed_dim, path=None):
            num_embeddings = len(dictionary)
            padding_idx = dictionary.pad()
            emb = Embedding(num_embeddings, embed_dim, padding_idx)
            # if provided, load from preloaded dictionaries
            if path:
                embed_dict = utils.parse_embedding(path)
                utils.load_embedding(embed_dict, dictionary, emb)
            return emb

        # though there is only one language for the LM, we still stick to encoder and decoder for clarity
        if args.share_all_embeddings:
            if args.encoder_embed_dim != args.decoder_embed_dim:
                raise ValueError(
                    '--share-all-embeddings requires --encoder-embed-dim to match --decoder-embed-dim')
            if args.decoder_embed_path and (
                    args.decoder_embed_path != args.encoder_embed_path):
                raise ValueError('--share-all-embeddings not compatible with --decoder-embed-path')
            encoder_embed_tokens = build_embedding(
                src_dict, args.encoder_embed_dim, args.encoder_embed_path
            )
            decoder_embed_tokens = encoder_embed_tokens
            args.share_decoder_input_output_embed = True
        else:
            encoder_embed_tokens = build_embedding(
                src_dict, args.encoder_embed_dim, args.encoder_embed_path
            )
            decoder_embed_tokens = build_embedding(
                src_dict, args.decoder_embed_dim, args.decoder_embed_path
            )

        text_encoder, text_conv_encoder, bottom_latent_encoder = cls.build_encoder(args, src_dict, encoder_embed_tokens)
        text_decoder = cls.build_decoder(args, src_dict, decoder_embed_tokens)

        if args.use_latent:
            bottom_quantizer = cls.build_quantizer(args)
        else:
            bottom_quantizer = None
        return VQVAE(args, text_encoder, text_conv_encoder, text_decoder, bottom_quantizer, bottom_latent_encoder)

    @classmethod
    def build_encoder(cls, args, src_dict, embed_tokens):
        text_encoder = TransformerEncoder(args, src_dict, embed_tokens, args.max_source_positions,
                                          args.encoder_layers, args.encoder_embed_dim, args.encoder_attention_heads,
                                          args.encoder_ffn_embed_dim)
        kernels, strides = parse_kernel_and_strides(args.bottom_conv_kernel_size, args.bottom_conv_stride)
        text_conv_encoder = ConvEncoder(args.encoder_embed_dim, kernels, strides, args.bottom_latent_dim)

        if args.use_bottom_quantants_encoder:
            bottom_latent_encoder = TransformerEncoder(args, None, None, args.max_source_positions,
                                            args.bottom_encoder_layers, args.bottom_latent_dim,
                                            args.bottom_encoder_attention_heads, args.bottom_encoder_ffn_embed_dim)
        else:
            bottom_latent_encoder = None
        return text_encoder, text_conv_encoder, bottom_latent_encoder

    @classmethod
    def build_quantizer(cls, args):
        bottom_quantizer = Quantize(args.bottom_latent_dim, args.bottom_latent_k)
        return bottom_quantizer

    @classmethod
    def build_decoder(cls, args, tgt_dict, embed_tokens):
        text_decoder = TransformerDecoder(
            args,
            tgt_dict,
            embed_tokens,
            args.decoder_embed_dim, args.decoder_attention_heads,
            args.decoder_ffn_embed_dim, args.decoder_output_dim,
            args.max_source_positions, args.decoder_layers,
            encoder_embed_dim=args.bottom_latent_dim,
            no_encoder_attn=getattr(args, 'no_cross_attention', False),
        )
        return text_decoder

    def mask_words(self, src_tokens, lengths):
        batch = src_tokens.size(0)
        src_masks = src_tokens.eq(self.pad_index)
        full_length = src_tokens.size(1)
        mask_lengths = (lengths.float() * self.word_drop_rate).long()
        mask = torch.arange(full_length).to(src_tokens.device).unsqueeze(0).expand(batch, full_length).ge(
            mask_lengths.unsqueeze(1))
        mask = mask.long()
        scores = src_tokens.clone().float().uniform_()
        scores.masked_fill(src_masks, 0)
        sorted_values, sorted_idx = torch.sort(scores, dim=-1, descending=True)
        mask = mask.scatter(1, sorted_idx, mask)
        src_tokens[(1 - mask).bool()] = self.pad_index
        return src_tokens

    def forward(self, decoder_tokens, lengths, full_tokens, update_steps, **kwargs):
        """
        output of text encoder
        {
            'encoder_out': x,  # T x B x C
            'encoder_padding_mask': encoder_padding_mask,  # B x T
            'encoder_embedding': encoder_embedding,  # B x T x C
            'encoder_states': encoder_states,  # List[T x B x C]
        }
        """
        text_encoder_out = self.text_encoder(full_tokens)
        encoding_mask = (~text_encoder_out['encoder_padding_mask']).type_as(text_encoder_out['encoder_out'])
        conv_inpt = text_encoder_out['encoder_out'] * (encoding_mask.transpose(0, 1).unsqueeze(-1)) # T x B x C
        # the output mask sets padding to be False
        # text_conv_out: T' x batch x C'
        # mask: batch x T'
        text_conv_out, mask = self.text_conv_encoder(conv_inpt.permute(1, 2, 0), lengths)  # B x C x T -> T' x B x C', C' = latent_dim

        if self.bottom_quantizer is not None and update_steps > self.pretrain_steps:
            # diff is the loss to update the enocder
            # quantize: masked T X batch x C; diff: scalar; embed_ind: T x batch
            quantize, diff, embed_ind, quantize_stats = self.bottom_quantizer(text_conv_out, mask.transpose(0, 1).contiguous())
        else:
            quantize = text_conv_out
            diff = text_conv_out.new_zeros(1)
            quantize_stats = {}

        if self.bottom_latent_encoder is not None:
            quantize = self.bottom_latent_encoder(src_encodings=quantize, encoder_padding_mask=~mask)
            quantize = quantize['encoder_out']

        quantize_out = {'encoder_out': quantize,  # masked T X batch x C
                        'encoder_padding_mask': ~mask,  # B x T, this mask sets padding to be True
                        'encoder_embedding': text_encoder_out['encoder_embedding']  # B x T x C
                        }

        if self.training and self.word_drop_rate > 0.0:
            decoder_tokens = self.mask_words(decoder_tokens, lengths)
        decoder_out = self.decoder(decoder_tokens, encoder_out=quantize_out)
        logits = decoder_out[0]
        return logits, diff, quantize_stats
        # todo: extract codes

        # todo: prior model - one decoder, one encoder-decoder
        # todo: sampling + task

        # todo: data set processing
        # todo: hierarchical model

    def extract_codes(self, full_tokens, full_lengths):
        text_encoder_out = self.text_encoder(full_tokens)
        encoding_mask = (~text_encoder_out['encoder_padding_mask']).type_as(text_encoder_out['encoder_out'])
        conv_inpt = text_encoder_out['encoder_out'] * (encoding_mask.transpose(0, 1).unsqueeze(-1))  # T x B x C
        text_conv_out, mask = self.text_conv_encoder(conv_inpt.permute(1, 2, 0),
                                                     full_lengths)  # B x C x T -> T' x B x C', C' = latent_dim

        # diff is the loss to update the enocder
        # quantize: masked T X batch x C; diff: scalar; embed_ind: T x batch
        quantize, diff, embed_ind, quantize_stats = self.bottom_quantizer(text_conv_out,
                                                                          mask.transpose(0, 1).contiguous())
        return embed_ind.transpose(0, 1).masked_fill(~mask, -1)


@register_model_architecture('vqvae_lm', 'vqvae_lm')
def base_architecture(args):
    args.encoder_embed_path = getattr(args, 'encoder_embed_path', None)
    args.encoder_embed_dim = getattr(args, 'encoder_embed_dim', 512)
    args.encoder_ffn_embed_dim = getattr(args, 'encoder_ffn_embed_dim', 2048)
    args.encoder_layers = getattr(args, 'encoder_layers', 6)
    args.encoder_attention_heads = getattr(args, 'encoder_attention_heads', 8)
    args.encoder_normalize_before = getattr(args, 'encoder_normalize_before', False)
    args.encoder_learned_pos = getattr(args, 'encoder_learned_pos', False)
    args.decoder_embed_path = getattr(args, 'decoder_embed_path', None)
    args.decoder_embed_dim = getattr(args, 'decoder_embed_dim', args.encoder_embed_dim)
    args.decoder_ffn_embed_dim = getattr(args, 'decoder_ffn_embed_dim', args.encoder_ffn_embed_dim)
    args.decoder_layers = getattr(args, 'decoder_layers', 6)
    args.decoder_attention_heads = getattr(args, 'decoder_attention_heads', 8)
    args.decoder_normalize_before = getattr(args, 'decoder_normalize_before', False)
    args.decoder_learned_pos = getattr(args, 'decoder_learned_pos', False)
    args.attention_dropout = getattr(args, 'attention_dropout', 0.)
    args.activation_dropout = getattr(args, 'activation_dropout', 0.)
    args.activation_fn = getattr(args, 'activation_fn', 'relu')
    args.dropout = getattr(args, 'dropout', 0.1)
    args.adaptive_softmax_cutoff = getattr(args, 'adaptive_softmax_cutoff', None)
    args.adaptive_softmax_dropout = getattr(args, 'adaptive_softmax_dropout', 0)
    args.share_decoder_input_output_embed = getattr(args, 'share_decoder_input_output_embed', False)
    args.share_all_embeddings = getattr(args, 'share_all_embeddings', False)
    args.no_token_positional_embeddings = getattr(args, 'no_token_positional_embeddings', False)
    args.adaptive_input = getattr(args, 'adaptive_input', False)
    args.no_cross_attention = getattr(args, 'no_cross_attention', False)
    args.cross_self_attention = getattr(args, 'cross_self_attention', False)
    args.layer_wise_attention = getattr(args, 'layer_wise_attention', False)

    args.decoder_output_dim = getattr(args, 'decoder_output_dim', args.decoder_embed_dim)
    args.decoder_input_dim = getattr(args, 'decoder_input_dim', args.decoder_embed_dim)

    args.bottom_conv_kernel_size = getattr(args, 'bottom_conv_kernel_size', '5,3')
    args.bottom_conv_stride = getattr(args, 'bottom_conv_stride', '2,2')
    args.bottom_latent_dim = getattr(args, 'bottom_latent_dim', args.encoder_embed_dim)
    args.bottom_latent_k = getattr(args, 'bottom_latent_k', 4096)

    args.use_bottom_quantants_encoder = getattr(args, 'use_bottom_quantants_encoder', 0)
    args.bottom_encoder_ffn_embed_dim = getattr(args, 'bottom_encoder_ffn_embed_dim', 1024)
    args.bottom_encoder_layers = getattr(args, 'bottom_encoder_layers', 3)
    args.bottom_encoder_attention_heads = getattr(args, 'bottom_encoder_attention_heads', 4)

    args.drop_word_prob = getattr(args, 'drop_word_prob', 0.0)
    args.use_latent = getattr(args, 'use_latent', 1)
    args.pretrain_steps = getattr(args, 'pretrain_steps', -1)

@register_model_architecture('vqvae_lm', "vqvae_lm_base")
def vqvae_base(args):
    base_architecture(args)