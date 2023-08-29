from collections import OrderedDict
import math
# import torch
import tensorrt as trt
import numpy as np
from tensorrt_llm._common import default_net
from tensorrt_llm._utils import pad_vocab_size, str_dtype_to_trt
from tensorrt_llm.functional import (
    RaggedTensor, Tensor, assertion, expand_mask, gather_last_token_logits,
    shape, concat, constant, gpt_attention, slice, concat, expand_dims_like
)
from tensorrt_llm.parameter import Parameter
from tensorrt_llm.layers import (Attention, AttentionMaskType, ColumnLinear, Embedding,
                       GatedMLP, PositionEmbeddingType, RmsNorm, RowLinear)
from tensorrt_llm.module import Module, ModuleList
from tensorrt_llm.quantization import QuantMode



class QWenAttention(Module):
    def __init__(
            self,
            hidden_size,
            num_attention_heads,
            max_position_embeddings,
            seq_length, # 2048
            num_layers=1,
            apply_query_key_layer_scaling=False,
            attention_mask_type=AttentionMaskType.causal,
            bias=True,
            dtype=None,
            position_embedding_type=PositionEmbeddingType.rope,
            neox_rotary_style=False,
            use_int8_kv_cache=False,
            rotary_embedding_percentage=1.0,
            tp_group=None,
            tp_size=1,
            multi_block_mode=False,
            multi_query_mode=False,
            use_dynamic_ntk=True,
            use_logn_attn=True,
            
        ):
        super().__init__()

        # max_positions = config.max_position_embeddings
        # self.register_buffer(
        #     "bias",
        #     torch.tril(
        #         torch.ones((max_positions, max_positions), dtype=torch.bool)
        #     ).view(1, 1, max_positions, max_positions),
        #     persistent=False,
        # )
        # self.register_buffer("masked_bias", torch.tensor(-1e4), persistent=False)

        self.seq_length = seq_length
        self.hidden_size = hidden_size
        self.split_size = hidden_size
        self.num_heads = num_attention_heads
        self.head_dim = self.hidden_size // self.num_heads

        # self.use_flash_attn = False
        self.scale_attn_weights = True

        self.projection_size = hidden_size
        self.hidden_size_per_attention_head = (
            self.projection_size // num_attention_heads
        )
        # copy from chatglm6b trt-llm
        self.attention_mask_type = attention_mask_type
        self.attention_head_size = hidden_size // num_attention_heads
        self.num_attention_heads = num_attention_heads // tp_size
        self.num_attention_kv_heads = 1 if multi_query_mode else self.num_attention_heads
        self.hidden_size = hidden_size // tp_size
        self.max_position_embeddings = max_position_embeddings

        self.num_layers = num_layers
        self.apply_query_key_layer_scaling = apply_query_key_layer_scaling
        self.norm_factor = math.sqrt(self.attention_head_size)
        self.q_scaling = 1
        if self.apply_query_key_layer_scaling:
            self.norm_factor *= self.num_layers
            self.q_scaling *= self.num_layers

        self.position_embedding_type = position_embedding_type
        self.multi_block_mode = multi_block_mode
        self.multi_query_mode = multi_query_mode

        self.rotary_embedding_dim = 0
        self.neox_rotary_style = neox_rotary_style
        if self.position_embedding_type == PositionEmbeddingType.rope:
            self.rotary_embedding_dim = int(self.attention_head_size *
                                            rotary_embedding_percentage)
            # TODO: Once we add RotaryEmbedding outside GPTAttention plugin,
            #       we need to set it up here

        self.dtype = dtype

        self.use_int8_kv_cache = use_int8_kv_cache
        if self.use_int8_kv_cache:
            self.kv_orig_quant_scale = Parameter(shape=(1, ), dtype='float32')
            self.kv_quant_orig_scale = Parameter(shape=(1, ), dtype='float32')
        else:
            self.register_parameter('kv_orig_quant_scale', None)
            self.register_parameter('kv_quant_orig_scale', None)
        # self.c_attn = nn.Linear(config.hidden_size, 3 * self.projection_size)

        # self.c_proj = nn.Linear(
        #     config.hidden_size, self.projection_size, bias=not config.no_bias
        # )
        self.qkv = ColumnLinear(
            hidden_size,
            hidden_size *
            3 if not multi_query_mode else hidden_size +
            2 * tp_size * self.attention_head_size,
            bias=True,
            dtype=dtype,
            tp_group=tp_group,
            tp_size=tp_size,
            gather_output=False
        )
        self.dense = RowLinear(
            hidden_size,
            hidden_size,
            bias=bias,
            dtype=dtype,
            tp_group=tp_group,
            tp_size=tp_size
        )

        # self.is_fp32 = not (dtype or config.fp16)
        # if (
        #     self.use_flash_attn
        #     and flash_attn_unpadded_func is not None
        #     and not self.is_fp32
        # ):
        #     self.core_attention_flash = FlashSelfAttention(
        #         causal=True, attention_dropout=config.attn_dropout_prob
        #     )
        # self.bf16 = config.bf16


        self.use_dynamic_ntk = use_dynamic_ntk
        self.use_logn_attn = use_logn_attn

        # torch implementation
        # logn_list = [
        #     math.log(i, self.seq_length) if i > self.seq_length else 1
        #     for i in range(1, 32768)
        # ]
        # self.logn_tensor = torch.tensor(logn_list)[None, :, None, None]
        # trt implementation move to QWenModel
        self.logn_tensor = Parameter(
            value=None,
            dtype=self.dtype,
            shape=[1, 32717, 1, 1],
        ) 
        # self.attn_dropout = nn.Dropout(config.attn_dropout_prob)

    # def _split_heads(self, tensor, num_heads, attn_head_size):
    #     new_shape = tensor.size()[:-1] + (num_heads, attn_head_size)
    #     tensor = tensor.view(new_shape)
    #     return tensor

    # def _merge_heads(self, tensor, num_heads, attn_head_size):
    #     tensor = tensor.contiguous()
    #     new_shape = tensor.size()[:-2] + (num_heads * attn_head_size,)
    #     return tensor.view(new_shape)

    def forward(
        self,
        hidden_states: RaggedTensor,
        rotary_pos_emb,
        past_key_value,
        sequence_length,
        past_key_value_length,
        masked_tokens,
        cache_indirection,
        # attention_mask: Optional[torch.FloatTensor] = None,
        # head_mask: Optional[torch.FloatTensor] = None,
        # encoder_hidden_states: Optional[torch.Tensor] = None,
        # encoder_attention_mask: Optional[torch.FloatTensor] = None,
        # output_attentions: Optional[bool] = False,
        use_cache=False
    ):
        if not default_net().plugin_config.gpt_attention_plugin:
            raise ValueError(
                'QWen is only supported with GPTAttention plugin')

        assert isinstance(hidden_states, RaggedTensor)
        input_lengths = hidden_states.row_lengths
        max_input_length = hidden_states.max_row_length
        hidden_states = hidden_states.data
        qkv = self.qkv(hidden_states)

        # torch implementation
        query, key, value = qkv.split(self.split_size, dim=2)

        # query = self._split_heads(query, self.num_heads, self.head_dim)
        query = query.view(
            concat([
                shape(qkv, 0),
                shape(qkv, 1),
                self.num_attention_heads,
                self.attention_head_size
            ]))
        # key = self._split_heads(key, self.num_heads, self.head_dim)
        key = key.view(
            concat([
                shape(qkv, 0),
                shape(qkv, 1),
                self.num_attention_heads,
                self.attention_head_size
            ]))
        # value = self._split_heads(value, self.num_heads, self.head_dim)
        value = value.view(
            concat([
                shape(qkv, 0),
                shape(qkv, 1),
                self.num_attention_heads,
                self.attention_head_size
            ]))
        # torch implementation
        # if rotary_pos_emb is not None:
        #     cur_len = query.shape[1]
        #     rotary_pos_emb = [i[:, -cur_len:, :, :] for i in rotary_pos_emb]
        #     rotary_pos_emb = (rotary_pos_emb,) * 2
        #     q_pos_emb, k_pos_emb = rotary_pos_emb
        #     # Slice the pos emb for current inference
        #     query = apply_rotary_pos_emb(query, q_pos_emb)
        #     key = apply_rotary_pos_emb(key, k_pos_emb)
        # trt implementation
        zero = constant(
            np.ascontiguousarray(
                np.zeros(
                    [1, 1, 1, 1],
                    dtype=np.float16
                    if self.dtype == trt.float16 else np.float32
                )
            )
        )
        def _rotate_half(x128):
            x64_part0, x64_part1 = x128.split(64, dim=-1)

            x64_part1_negtive = zero - x64_part1

            y64 = concat([x64_part1_negtive, x64_part0], dim=3)
            return y64

        def apply_rotary_pos_emb(t, freqs):
            cos1, sin1 = freqs
            t_ = t.cast(trt.float32)
            t_rotate = _rotate_half(t_)
            y128 = t_ * cos1 + t_rotate * sin1
            # y128 = y128.view(shape(x))
            y128 = y128.cast(t.dtype)
            return y128
        q_pos_emb, k_pos_emb = rotary_pos_emb
        query = apply_rotary_pos_emb(query, q_pos_emb)
        key = apply_rotary_pos_emb(key, k_pos_emb)

        # this code will implement in trt
        # if layer_past is not None:
        #     past_key, past_value = layer_past[0], layer_past[1]
        #     key = torch.cat((past_key, key), dim=1)
        #     value = torch.cat((past_value, value), dim=1)

        kv_orig_quant_scale = self.kv_orig_quant_scale.value if self.use_int8_kv_cache else None
        kv_quant_orig_scale = self.kv_quant_orig_scale.value if self.use_int8_kv_cache else None

        qkv = concat([query, key, value], dim=2)
        qkv = qkv.view(
            concat([shape(qkv, 0),
                    shape(qkv, 1),
                    self.hidden_size * 3])
        )
        # will implement in trt
        # if use_cache:
        #     present = (key, value)
        # else:
        #     present = None

        # implement in tensor
        # if self.use_logn_attn and not self.training:
        #     if self.logn_tensor.device != query.device or self.logn_tensor.dtype != query.dtype:
        #         self.logn_tensor = self.logn_tensor.to(query.device).type_as(query)
        #     seq_start = key.size(1) - query.size(1)
        #     seq_end = key.size(1)
        #     logn_tensor = self.logn_tensor[:, seq_start:seq_end, :, :]
        #     query = query * logn_tensor.expand_as(query)
        # implement in trt
        seq_start = slice(shape(key), [1], [1]) - slice(shape(query), [1], [1])
        seq_end = slice(shape(key), [1], [1])
        logn_shape = self.logn_tensor.value.shape
        logn_tensor = slice(
            input=self.logn_tensor.value,
            starts=concat([0, seq_start, 0, 0]),
            sizes=concat([logn_shape[0], seq_end - seq_start, logn_shape[2], logn_shape[3]]),
        )
        query = query * expand_dims_like(logn_tensor, query)

        # flash attention implementation 
        # if (
        #     self.use_flash_attn
        #     and flash_attn_unpadded_func is not None
        #     and not self.is_fp32
        #     and query.is_cuda
        # ):
        #     q, k, v = query, key, value
        #     context_layer = self.core_attention_flash(q, k, v)

        #     # b s h d -> b s (h d)
        #     context_layer = context_layer.flatten(2,3).contiguous()

        # else:
        # gpt attention implementation with torch
            # query = query.permute(0, 2, 1, 3)
            # key = key.permute(0, 2, 1, 3)
            # value = value.permute(0, 2, 1, 3)
            # attn_output, attn_weight = self._attn(
            #     query, key, value, attention_mask, head_mask
            # )
            # context_layer = self._merge_heads(
            #     attn_output, self.num_heads, self.head_dim
            # )

        # attn_output = self.c_proj(context_layer)

        # outputs = (attn_output, present)
        # if output_attentions:
        #     if (
        #         self.use_flash_attn
        #         and flash_attn_unpadded_func is not None
        #         and not self.is_fp32
        #     ):
        #         raise ValueError("Cannot output attentions while using flash-attn")
        #     else:
        #         outputs += (attn_weight,)

        # return outputs
        context, past_key_value = gpt_attention(
            qkv,
            past_key_value,
            sequence_length,
            past_key_value_length,
            masked_tokens,
            input_lengths,
            max_input_length,
            cache_indirection,
            self.num_attention_heads,
            self.attention_head_size,
            self.q_scaling,
            self.rotary_embedding_dim,
            self.neox_rotary_style,
            self.multi_block_mode,
            self.multi_query_mode,
            kv_orig_quant_scale,
            kv_quant_orig_scale,
            self.use_int8_kv_cache,
            mask_type=self.attention_mask_type.value)

        context = self.dense(context)

        context = RaggedTensor.from_row_lengths(context, input_lengths,
                                                max_input_length)
        if use_cache:
            return (context, past_key_value)
        else:
            return context


class QWenBlock(Module):

    def __init__(self,
                 layer_id,
                 hidden_size,
                 seq_length,
                 num_attention_heads,
                 max_position_embeddings,
                 dtype=None,
                 hidden_act='silu',
                 position_embedding_type=PositionEmbeddingType.rope,
                 quant_mode=QuantMode(0),
                 mlp_hidden_size=None,
                 neox_rotary_style=True,
                 bias=False,
                 multi_query_mode=False,
                 tp_group=None,
                 tp_size=1):
        super().__init__()
        self._layer_id = layer_id  # useful for debugging
        self.ln_1 = RmsNorm(normalized_shape=hidden_size,
                                       dtype=dtype)

        self.attention = QWenAttention(
            hidden_size=hidden_size,
            num_attention_heads=num_attention_heads,
            max_position_embeddings=max_position_embeddings,
            seq_length=seq_length,
            dtype=dtype,
            attention_mask_type=AttentionMaskType.causal,
            bias=bias,
            position_embedding_type=position_embedding_type,
            neox_rotary_style=neox_rotary_style,
            multi_query_mode=multi_query_mode,
            tp_group=tp_group,
            tp_size=tp_size,
            use_int8_kv_cache=quant_mode.has_int8_kv_cache()
        )
        if not mlp_hidden_size:
            mlp_hidden_size = hidden_size * 4
        self.mlp = GatedMLP(hidden_size=hidden_size,
                            ffn_hidden_size=mlp_hidden_size // 2,
                            hidden_act=hidden_act,
                            dtype=dtype,
                            bias=False,
                            tp_group=tp_group,
                            tp_size=tp_size)
        self.ln_2 = RmsNorm(normalized_shape=hidden_size, dtype=dtype)

    def forward(self,
                hidden_states: RaggedTensor,
                rotary_pos_emb,
                past_key_value=None,
                sequence_length=None,
                past_key_value_length=None,
                masked_tokens=None,
                use_cache=False,
                cache_indirection=None):
        residual = hidden_states.data
        input_lengths = hidden_states.row_lengths
        max_input_length = hidden_states.max_row_length
        hidden_states = self.ln_1(hidden_states.data)

        attention_output = self.attention(
            RaggedTensor.from_row_lengths(hidden_states, input_lengths,
                                          max_input_length),
            rotary_pos_emb=rotary_pos_emb,
            past_key_value=past_key_value,
            sequence_length=sequence_length,
            past_key_value_length=past_key_value_length,
            masked_tokens=masked_tokens,
            cache_indirection=cache_indirection,
            use_cache=use_cache,
        )

        if use_cache:
            attention_output, presents = attention_output

        hidden_states = residual + attention_output.data

        residual = hidden_states
        hidden_states = self.ln_2(hidden_states)

        hidden_states = self.mlp(hidden_states)

        hidden_states = residual + hidden_states
        hidden_states = RaggedTensor.from_row_lengths(
            hidden_states, attention_output.row_lengths,
            attention_output.max_row_length)
        if use_cache:
            return (hidden_states, presents)
        return hidden_states


class QWenModel(Module):

    def __init__(self,
        num_layers,
        num_heads,
        hidden_size,
        seq_length,
        vocab_size,
        hidden_act,
        max_position_embeddings,
        dtype,
        mlp_hidden_size=None,
        neox_rotary_style=True,
        tensor_parallel=1,
        tensor_parallel_group=None,
        bias=False,
        quant_mode=QuantMode(0),
        multi_query_mode=False,
    ):
        super().__init__()
        self.vocab_embedding = Embedding(vocab_size, hidden_size, dtype=dtype)
        # copy from chatglm
        self.head_size = hidden_size // num_heads
        self.position_embedding_cos = Embedding(
            max_position_embeddings,
            self.head_size,
            dtype=trt.float32
        )
        self.position_embedding_sin = Embedding(
            max_position_embeddings,
            self.head_size,
            dtype=trt.float32
        )

        self.layers = ModuleList([
            QWenBlock(
                layer_id=i,
                hidden_size=hidden_size,
                seq_length=seq_length,
                num_attention_heads=num_heads,
                max_position_embeddings=max_position_embeddings,
                dtype=dtype,
                hidden_act=hidden_act,
                mlp_hidden_size=mlp_hidden_size,
                neox_rotary_style=neox_rotary_style,
                bias=bias,
                quant_mode=quant_mode,
                multi_query_mode=multi_query_mode,
                tp_group=tensor_parallel_group,
                tp_size=tensor_parallel,
            )
            for i in range(num_layers)
        ])

        self.ln_f = RmsNorm(normalized_shape=hidden_size, dtype=dtype)

    def forward(self,
                input_ids: RaggedTensor,
                position_ids=None,
                past_key_value=None,
                sequence_length=None,
                past_key_value_length=None,
                masked_tokens=None,
                use_cache=False,
                attention_mask=None,
                cache_indirection=None):

        hidden_states = self.vocab_embedding(input_ids.data)

        # copy from chatglm6b
        batch_size = shape(input_ids.data, 0)
        input_len = shape(input_ids.data, 1)
        position_embedding_cos = self.position_embedding_cos(position_ids)
        position_embedding_sin = self.position_embedding_sin(position_ids)
        position_embedding_cos = position_embedding_cos.view(
            concat([batch_size, input_len, 1, self.head_size]))
        position_embedding_sin = position_embedding_sin.view(
            concat([batch_size, input_len, 1, self.head_size]))
        rotary_pos_emb = [
            (position_embedding_cos, position_embedding_sin), 
            (position_embedding_cos, position_embedding_sin), 
        ]

        if past_key_value is None:
            past_key_value = tuple([None] * len(self.layers))

        if use_cache:
            presents = []

        if attention_mask is not None:
            attention_mask = expand_mask(attention_mask,
                                         shape(input_ids.data, -1))

        hidden_states = RaggedTensor.from_row_lengths(hidden_states,
                                                      input_ids.row_lengths,
                                                      input_ids.max_row_length)

        for layer, past in zip(self.layers, past_key_value):
            hidden_states = layer(
                hidden_states,
                rotary_pos_emb=rotary_pos_emb,
                past_key_value=past,
                sequence_length=sequence_length,
                past_key_value_length=past_key_value_length,
                masked_tokens=masked_tokens,
                use_cache=use_cache,
                # attention_mask=attention_mask,
                cache_indirection=cache_indirection
            )
            if use_cache:
                presents.append(hidden_states[1])
                hidden_states = hidden_states[0]

        hidden_states = self.ln_f(hidden_states.data)

        if use_cache:
            return (hidden_states, tuple(presents))
        return hidden_states


class QWenForCausalLM(QWenModel):

    def __init__(self,
        num_layers,
        num_heads,
        hidden_size,
        seq_length,
        vocab_size,
        hidden_act,
        max_position_embeddings,
        dtype,
        mlp_hidden_size=None,
        neox_rotary_style=True,
        tensor_parallel=1,
        tensor_parallel_group=None,
        multi_query_mode=False
    ):
        if isinstance(dtype, str):
            self.kv_dtype = str_dtype_to_trt(dtype)
        else:
            assert isinstance(dtype, trt.DataType)
            self.kv_dtype = dtype
        self.num_layers = num_layers
        self.num_heads = num_heads
        self.hidden_size = hidden_size
        self.vocab_size = vocab_size
        self.tensor_parallel = tensor_parallel
        self._multi_query_mode = multi_query_mode
        super().__init__(num_layers, num_heads, hidden_size, seq_length, 
                         vocab_size, hidden_act, max_position_embeddings, dtype,
                         mlp_hidden_size, neox_rotary_style, tensor_parallel,
                         tensor_parallel_group, multi_query_mode)
        vocab_size_padded = pad_vocab_size(vocab_size, tensor_parallel)
        self.lm_head = ColumnLinear(hidden_size,
                                    vocab_size_padded,
                                    bias=False,
                                    dtype=dtype,
                                    tp_group=tensor_parallel_group,
                                    tp_size=tensor_parallel,
                                    gather_output=True)

    def forward(self,
                input_ids: RaggedTensor,
                position_ids=None,
                past_key_value=None,
                sequence_length=None,
                past_key_value_length=None,
                masked_tokens=None,
                use_cache=False,
                last_token_ids=None,
                attention_mask=None,
                cache_indirection=None):
        hidden_states = super().forward(input_ids, position_ids, past_key_value,
                                        sequence_length, past_key_value_length,
                                        masked_tokens, use_cache,
                                        attention_mask, cache_indirection)

        if use_cache:
            hidden_states, presents = hidden_states

        hidden_states = gather_last_token_logits(
            hidden_states, last_token_ids,
            default_net().plugin_config.remove_input_padding)

        # [batch_size, hidden_size] -> [batch_size, vocab_size]
        lm_logits = self.lm_head(hidden_states)
        lm_logits.mark_output('logits', self.kv_dtype)

        if use_cache:
            for i, present in enumerate(presents):
                present.mark_output(f'present_key_value_{i}', self.kv_dtype)
            return (lm_logits, presents)

        return lm_logits

    def prepare_inputs(self, max_batch_size, max_input_len, max_new_tokens,
                       use_cache, max_beam_width):
        '''@brief: Prepare inputs Tensors for the model, the given sizes are used to determine the
            ranges of the dimensions of when using TRT dynamic shapes.

            @return: a list contains values which can be fed into the self.forward()
        '''

        # Prepare inputs
        head_size = self.hidden_size // self.num_heads
        num_heads = self.num_heads // self.tensor_parallel
        num_heads_kv = 1 if self._multi_query_mode else num_heads
        max_len = max_input_len + max_new_tokens
        bb_range = [
            1, (max_batch_size * max_beam_width + 1) // 2,
            max_batch_size * max_beam_width
        ]
        bs_range = [1, (max_batch_size + 1) // 2, max_batch_size]
        beam_width_range = [1, (max_beam_width + 1) // 2, max_beam_width]
        inlen_range = [1, 1, max_input_len]
        max_len_range = [0, (max_len + 1) // 2, max_len]
        mask_len_range = [1, (max_len + 1) // 2 + 1, max_len + 1]
        num_tokens_range = [
            1, max_batch_size * max_beam_width,
            max(max_input_len * max_batch_size, max_beam_width * max_batch_size)
        ]

        past_key_value = []
        sequence_length = None
        past_key_value_length = None
        masked_tokens = None
        attention_mask = None
        use_gpt_attention_plugin = default_net(
        ).plugin_config.gpt_attention_plugin
        remove_input_padding = default_net().plugin_config.remove_input_padding

        if remove_input_padding:
            input_ids = Tensor(name='input_ids',
                               dtype=trt.int32,
                               shape=[1, -1],
                               dim_range=OrderedDict([
                                   ('batch_size', [1]),
                                   ('num_tokens', [num_tokens_range]),
                               ]))
            position_ids = Tensor(name='position_ids',
                                  dtype=trt.int32,
                                  shape=[1, -1],
                                  dim_range=OrderedDict([
                                      ('batch_size', [1]),
                                      ('num_tokens', [num_tokens_range]),
                                  ]))
        else:
            input_ids = Tensor(name='input_ids',
                               dtype=trt.int32,
                               shape=[-1, -1],
                               dim_range=OrderedDict([
                                   ('batch_size', [bb_range]),
                                   ('input_len', [inlen_range]),
                               ]))
            position_ids = Tensor(name='position_ids',
                                  dtype=trt.int32,
                                  shape=[-1, -1],
                                  dim_range=OrderedDict([
                                      ('batch_size', [bb_range]),
                                      ('input_len', [inlen_range]),
                                  ]))

        for i in range(self.num_layers):
            kv_dim_range = OrderedDict([
                ('batch_size', [bb_range]),
                ('kv', [2]),
                ('num_heads', [num_heads_kv]),
                ('past_key_len', [max_len_range]),
                ('head_size', [head_size]),
            ])
            kv = Tensor(name=f'past_key_value_{i}',
                        dtype=self.kv_dtype,
                        shape=[-1, 2, num_heads_kv, -1, head_size],
                        dim_range=kv_dim_range)
            past_key_value.append(kv)
            # TODO(kaiyu): Remove this when TRT fix the named dimension
            if not remove_input_padding:
                assertion(shape(input_ids, 0) == shape(kv, 0), 'batch size')

        if use_gpt_attention_plugin:
            sequence_length = Tensor(
                name='sequence_length',
                dtype=trt.int32,
                shape=[-1],
                dim_range=OrderedDict([('batch_size', [bb_range])]),
            )
            past_key_value_length = Tensor(
                name='past_key_value_length',
                dtype=trt.int32,
                shape=[-1],
                dim_range=OrderedDict([('past_key_value_length',
                                        [max_len_range])]),
            )
            masked_tokens = Tensor(name='masked_tokens',
                                   dtype=trt.int32,
                                   shape=[-1, -1],
                                   dim_range=OrderedDict([
                                       ('batch_size', [bb_range]),
                                       ('max_seq_len', [max_len_range]),
                                   ]))
        else:
            attention_mask = Tensor(name='attention_mask',
                                    dtype=trt.int32,
                                    shape=[-1, -1],
                                    dim_range=OrderedDict([
                                        ('batch_size', [bb_range]),
                                        ('mask_len', [mask_len_range]),
                                    ]))

        input_lengths = Tensor(name='input_lengths',
                               dtype=trt.int32,
                               shape=[-1],
                               dim_range=OrderedDict([('batch_size', [bb_range])
                                                      ]))

        max_input_length = Tensor(name='max_input_length',
                                  dtype=trt.int32,
                                  shape=[-1],
                                  dim_range=OrderedDict([('max_input_len',
                                                          [inlen_range])]))

        last_token_ids = Tensor(name='last_token_ids',
                                dtype=trt.int32,
                                shape=[-1],
                                dim_range=OrderedDict([
                                    ('batch_size', [bb_range]),
                                ]))
        input_ids_ragged = RaggedTensor.from_row_lengths(
            input_ids, input_lengths, max_input_length)

        cache_indirection = Tensor(name='cache_indirection',
                                   dtype=trt.int32,
                                   shape=[-1, -1, -1],
                                   dim_range=OrderedDict([
                                       ('batch_size', [bs_range]),
                                       ('beam_width', [beam_width_range]),
                                       ('max_seq_len', [max_len_range]),
                                   ]))

        return (input_ids_ragged, position_ids, past_key_value, sequence_length,
                past_key_value_length, masked_tokens, True, last_token_ids,
                attention_mask, cache_indirection)