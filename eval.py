"""评估训练保存模型的准确率"""

import argparse
import functools
import time

import paddle.fluid as fluid
from data_utils.data import DataGenerator
from model_utils.model import DeepSpeech2Model
from model_utils.model_check import check_cuda, check_version
from utils.error_rate import char_errors, word_errors
from utils.utility import add_arguments, print_arguments

parser = argparse.ArgumentParser(description=__doc__)
add_arg = functools.partial(add_arguments, argparser=parser)
add_arg('batch_size',       int,    64,     "评估是每一批数据的大小")
add_arg('beam_size',        int,    10,     "定向搜索的大小，范围:[5, 500]")
add_arg('num_conv_layers',  int,    2,      "卷积层数量")
add_arg('num_rnn_layers',   int,    3,      "循环神经网络的数量")
add_arg('rnn_layer_size',   int,    2048,   "循环神经网络的大小")
add_arg('alpha',            float,  1.2,    "定向搜索的LM系数")
add_arg('num_proc_bsearch', int,    8,     "定向搜索方法使用CPU数量")
add_arg('beta',             float,  0.35,   "定向搜索的WC系数")
add_arg('cutoff_prob',      float,  1.0,    "剪枝的概率")
add_arg('cutoff_top_n',     int,    40,     "剪枝的最大值")
add_arg('use_gru',          bool,   True,   "是否使用GRUs模型，不使用RNN")
add_arg('use_gpu',          bool,   True,   "是否使用GPU训练")
add_arg('share_rnn_weights',bool,   False,   "是否在RNN上共享权重")
add_arg('test_manifest',    str,    './dataset/manifest.test',     "需要评估的测试数据列表")
add_arg('mean_std_path',    str,    './dataset/mean_std.npz',      "数据集的均值和标准值的npy文件路径")
add_arg('vocab_path',       str,    './dataset/zh_vocab.txt',      "数据集的字典文件路径")
add_arg('model_path',       str,    './models/step_final/',        "训练保存的模型文件夹路径")
add_arg('lang_model_path',  str,    './lm/zh_giga.no_cna_cmn.prune01244.klm',        "语言模型文件路径")
add_arg('decoding_method',  str,    'ctc_beam_search',        "结果解码方法，有定向搜索(ctc_beam_search)、贪婪策略(ctc_greedy)", choices=['ctc_beam_search', 'ctc_greedy'])
add_arg('error_rate_type',  str,    'cer',    "评估所使用的错误率方法，有字错率(cer)、词错率(wer)", choices=['wer', 'cer'])
add_arg('specgram_type',    str,    'linear', "对音频的预处理方式，有: linear, mfcc", choices=['linear', 'mfcc'])
args = parser.parse_args()


# 评估模型
def evaluate():
    # 检测PaddlePaddle环境
    check_cuda(args.use_gpu)
    check_version()

    # 是否使用GPU
    place = fluid.CUDAPlace(0) if args.use_gpu else fluid.CPUPlace()

    # 获取数据生成器
    data_generator = DataGenerator(vocab_filepath=args.vocab_path,
                                   mean_std_filepath=args.mean_std_path,
                                   augmentation_config='{}',
                                   specgram_type=args.specgram_type,
                                   keep_transcription_text=True,
                                   place=place,
                                   is_training=False)
    # 获取评估数据
    batch_reader = data_generator.batch_reader_creator(manifest_path=args.test_manifest,
                                                       batch_size=args.batch_size,
                                                       shuffle_method=None)
    # 获取DeepSpeech2模型，并设置为预测
    ds2_model = DeepSpeech2Model(vocab_size=data_generator.vocab_size,
                                 num_conv_layers=args.num_conv_layers,
                                 num_rnn_layers=args.num_rnn_layers,
                                 rnn_layer_size=args.rnn_layer_size,
                                 use_gru=args.use_gru,
                                 share_rnn_weights=args.share_rnn_weights,
                                 place=place,
                                 init_from_pretrained_model=args.model_path,
                                 is_infer=True)

    # 读取数据列表
    with open(args.test_manifest, 'r', encoding='utf-8') as f_m:
        test_len = len(f_m.readlines())

    # 定向搜索方法的处理
    if args.decoding_method == "ctc_beam_search":
        ds2_model.init_ext_scorer(args.alpha, args.beta, args.lang_model_path, data_generator.vocab_list)

    # 获取评估函数，有字错率和词错率
    errors_func = char_errors if args.error_rate_type == 'cer' else word_errors
    errors_sum, len_refs, num_ins = 0.0, 0, 0
    ds2_model.logger.info("开始评估 ...")
    start = time.time()
    # 开始评估
    for infer_data in batch_reader():
        # 获取一批的识别结果
        probs_split = ds2_model.infer_batch_probs(infer_data=infer_data)

        # 执行解码
        if args.decoding_method == "ctc_greedy":
            # 最优路径解码
            result_transcripts = ds2_model.decode_batch_greedy(probs_split=probs_split,
                                                               vocab_list=data_generator.vocab_list)
        else:
            # 定向搜索解码
            result_transcripts = ds2_model.decode_batch_beam_search(probs_split=probs_split,
                                                                    beam_alpha=args.alpha,
                                                                    beam_beta=args.beta,
                                                                    beam_size=args.beam_size,
                                                                    cutoff_prob=args.cutoff_prob,
                                                                    cutoff_top_n=args.cutoff_top_n,
                                                                    vocab_list=data_generator.vocab_list,
                                                                    num_processes=args.num_proc_bsearch)
        target_transcripts = infer_data[1]

        # 计算字错率
        for target, result in zip(target_transcripts, result_transcripts):
            errors, len_ref = errors_func(target, result)
            errors_sum += errors
            len_refs += len_ref
            num_ins += 1
        print("错误率：[%s] (%d/%d) = %f" % (args.error_rate_type, num_ins, test_len, errors_sum / len_refs))
    end = time.time()
    print("消耗时间：%ds, 总错误率：[%s] (%d/%d) = %f" % ((end - start), args.error_rate_type, num_ins, num_ins, errors_sum / len_refs))

    ds2_model.logger.info("完成评估！")


def main():
    print_arguments(args)
    evaluate()


if __name__ == '__main__':
    main()
